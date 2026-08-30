import base64
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from conftest import _truncate_application_tables
from pydantic import ValidationError

from app.config import Settings
from app.errors import LedgerError
from app.pagination import CursorKind, decode_cursor, encode_cursor
from app.schemas import DepositCreate, TransferCreate

MAX_NUMERIC_20_2 = "999999999999999999.99"
TOO_LARGE_FOR_NUMERIC_20_2 = "1000000000000000000.00"


def test_truncation_refuses_a_non_test_database_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEDGERLITE_ALLOW_UNSAFE_TEST_DATABASE", raising=False)
    engine = Mock()
    engine.url.database = "ledgerlite"

    with pytest.raises(RuntimeError, match="test database names must end"):
        _truncate_application_tables(engine)
    engine.begin.assert_not_called()


def test_money_accepts_the_numeric_20_2_upper_bound() -> None:
    assert DepositCreate(amount=MAX_NUMERIC_20_2).amount == Decimal(MAX_NUMERIC_20_2)


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (DepositCreate, {"amount": TOO_LARGE_FOR_NUMERIC_20_2}),
        (
            TransferCreate,
            {
                "source_account_id": uuid4(),
                "destination_account_id": uuid4(),
                "amount": TOO_LARGE_FOR_NUMERIC_20_2,
            },
        ),
    ],
)
def test_money_rejects_values_larger_than_numeric_20_2(
    schema: type[DepositCreate] | type[TransferCreate], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="amount exceeds NUMERIC\\(20, 2\\)"):
        schema.model_validate(payload)


class _State(Enum):
    OPEN = "open"


def _token(value: object) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _payload(token: str) -> dict[str, object]:
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


@pytest.mark.parametrize(
    ("kind", "position"),
    [
        (CursorKind.ACCOUNTS, (datetime(2026, 1, 1, tzinfo=UTC), uuid4())),
        (CursorKind.RECONCILIATION_ITEMS, (datetime.now(UTC), uuid4())),
        (CursorKind.TRANSACTIONS, (42, uuid4())),
        (CursorKind.STATEMENT, (42, uuid4(), 2)),
    ],
)
def test_pagination_cursors_round_trip_each_ordering_contract(
    kind: CursorKind, position: tuple[object, ...]
) -> None:
    filters = {
        "id": uuid4(),
        "at": datetime(2026, 1, 1, tzinfo=UTC),
        "day": date(2026, 1, 1),
        "amount": Decimal("10.20"),
        "state": _State.OPEN,
        "nested": [None, True, 3],
    }
    cursor = encode_cursor(
        kind=kind,
        filters=filters,
        position=position,  # type: ignore[arg-type]
        high_water=position,  # type: ignore[arg-type]
    )
    decoded = decode_cursor(cursor, kind=kind, filters=filters)
    assert decoded.position == decoded.high_water == position


def test_pagination_cursors_reject_transport_context_and_key_tampering() -> None:
    row_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    cursor = encode_cursor(
        kind=CursorKind.ACCOUNTS,
        filters={"currency": "AED"},
        position=(datetime(2026, 1, 1, tzinfo=UTC), row_id),
    )
    for malformed in (
        "",
        "A",
        "not+urlsafe",
        "a" * 2049,
        _token(b"\xff"),
        _token(b"not-json"),
        _token([]),
        _token({"v": 1}),
    ):
        with pytest.raises(LedgerError):
            decode_cursor(
                malformed, kind=CursorKind.ACCOUNTS, filters={"currency": "AED"}
            )

    original = _payload(cursor)
    for field, value in (
        ("v", True),
        ("v", 2),
        ("k", "transactions"),
        ("f", "bad"),
        ("p", []),
        ("p", ["2026-01-01T00:00:00+00:00", str(row_id)]),
        ("p", ["2026-01-01T00:00:00.000000Z", "not-a-uuid"]),
    ):
        tampered = dict(original)
        tampered[field] = value
        with pytest.raises(LedgerError):
            decode_cursor(
                _token(tampered),
                kind=CursorKind.ACCOUNTS,
                filters={"currency": "AED"},
            )
    with pytest.raises(LedgerError, match="active filters"):
        decode_cursor(cursor, kind=CursorKind.ACCOUNTS, filters={"currency": "USD"})

    posting = encode_cursor(
        kind=CursorKind.TRANSACTIONS, filters={}, position=(1, row_id)
    )
    for bad_sequence in (1, "0", "01", "9223372036854775808"):
        tampered = _payload(posting)
        tampered["p"] = [bad_sequence, str(row_id)]
        with pytest.raises(LedgerError):
            decode_cursor(_token(tampered), kind=CursorKind.TRANSACTIONS, filters={})


def test_pagination_cursor_rejects_invalid_server_contracts() -> None:
    samples = (
        {"kind": "missing", "filters": {}, "position": (1, uuid4())},
        {"kind": CursorKind.ACCOUNTS, "filters": {}, "position": [1, uuid4()]},
        {"kind": CursorKind.TRANSACTIONS, "filters": {}, "position": (0, uuid4())},
        {
            "kind": CursorKind.ACCOUNTS,
            "filters": {"amount": Decimal("Infinity")},
            "position": (datetime.now(UTC), uuid4()),
        },
    )
    for kwargs in samples:
        with pytest.raises((TypeError, ValueError)):
            encode_cursor(**kwargs)  # type: ignore[arg-type]


def test_settings_require_psycopg() -> None:
    assert Settings(_env_file=None, app_env="local").app_env == "local"
    for url in ("not a url", "sqlite:///ledger.db", "postgresql://db/app"):
        with pytest.raises(ValidationError, match="DATABASE_URL"):
            Settings(_env_file=None, database_url=url)
