"""Opaque, filter-bound keyset pagination cursors.

Cursors are an API transport detail, not durable storage.  Their payload is
versioned so its representation can evolve without accidentally accepting a
cursor produced for a different query or resource.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from uuid import UUID

from app.errors import LedgerError

CURSOR_VERSION = 1
MAX_CURSOR_LENGTH = 2_048

_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class CursorKind(StrEnum):
    """The query whose ordering contract a cursor belongs to."""

    ACCOUNTS = "accounts"
    TRANSACTIONS = "transactions"
    STATEMENT = "statement"
    RECONCILIATION_ITEMS = "reconciliation_items"


type TimestampCursorKey = tuple[datetime, UUID]
type TransactionCursorKey = tuple[int, UUID]
type TwoPartCursorKey = TimestampCursorKey | TransactionCursorKey
type StatementCursorKey = tuple[int, UUID, int]
type CursorKey = TwoPartCursorKey | StatementCursorKey


@dataclass(frozen=True, slots=True)
class DecodedCursor:
    """Validated cursor state ready to use in keyset predicates."""

    position: CursorKey
    high_water: CursorKey | None


def encode_cursor(
    *,
    kind: CursorKind,
    filters: Mapping[str, object],
    position: CursorKey,
    high_water: CursorKey | None = None,
) -> str:
    """Encode a keyset position and its optional initial-query high-water key.

    ``filters`` are hashed into the payload.  This binds a cursor to the exact
    normalized filter set without exposing those values in the token.

    Invalid arguments indicate a server-side programming error and raise
    ``TypeError`` or ``ValueError``.  User-provided cursors are validated by
    :func:`decode_cursor` and raise :class:`~app.errors.LedgerError` instead.
    """

    cursor_kind = _coerce_kind(kind)
    payload = {
        "f": _filter_fingerprint(filters),
        "h": (_encode_key(cursor_kind, high_water) if high_water is not None else None),
        "k": cursor_kind.value,
        "p": _encode_key(cursor_kind, position),
        "v": CURSOR_VERSION,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = _base64url_encode(raw)
    if len(encoded) > MAX_CURSOR_LENGTH:
        raise ValueError("encoded cursor exceeds the supported length")
    return encoded


def decode_cursor(
    cursor: str,
    *,
    kind: CursorKind,
    filters: Mapping[str, object],
) -> DecodedCursor:
    """Decode and validate an untrusted pagination cursor.

    The token must be canonical URL-safe base64 JSON and match the expected
    version, resource kind, key shape, and active query filters.
    """

    expected_kind = _coerce_kind(kind)
    raw = _decode_transport(cursor)
    payload = _decode_payload(raw)

    if type(payload.get("v")) is not int:
        raise _malformed_cursor()
    if payload["v"] != CURSOR_VERSION:
        raise LedgerError(422, "cursor version is unsupported")

    payload_kind = payload.get("k")
    if not isinstance(payload_kind, str):
        raise _malformed_cursor()
    if payload_kind != expected_kind.value:
        raise LedgerError(422, "cursor does not match this resource")

    fingerprint = payload.get("f")
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise _malformed_cursor()
    if not hmac.compare_digest(fingerprint, _filter_fingerprint(filters)):
        raise LedgerError(422, "cursor does not match the active filters")

    position = _decode_key(expected_kind, payload.get("p"))
    high_water_value = payload.get("h")
    high_water = (
        None
        if high_water_value is None
        else _decode_key(expected_kind, high_water_value)
    )
    if high_water is not None and high_water < position:
        raise _malformed_cursor()
    return DecodedCursor(position=position, high_water=high_water)


def _coerce_kind(kind: CursorKind) -> CursorKind:
    if isinstance(kind, CursorKind):
        return kind
    try:
        return CursorKind(kind)
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported cursor kind") from exc


def _decode_transport(cursor: str) -> bytes:
    if not isinstance(cursor, str) or not cursor:
        raise _malformed_cursor()
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise LedgerError(422, "cursor exceeds the maximum length")
    if not _BASE64URL_RE.fullmatch(cursor):
        raise _malformed_cursor()

    padding = b"=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(
            cursor.encode("ascii") + padding,
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise _malformed_cursor() from exc
    if not raw or _base64url_encode(raw) != cursor:
        raise _malformed_cursor()
    return raw


def _decode_payload(raw: bytes) -> dict[str, object]:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _malformed_cursor() from exc

    if not isinstance(payload, dict):
        raise _malformed_cursor()
    if set(payload) != {"f", "h", "k", "p", "v"}:
        raise _malformed_cursor()
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


def _encode_key(kind: CursorKind, key: CursorKey) -> list[object]:
    expected_parts = 3 if kind is CursorKind.STATEMENT else 2
    if not isinstance(key, tuple) or len(key) != expected_parts:
        raise TypeError(f"{kind.value} cursor key must have {expected_parts} parts")

    ordering_value, row_id, *remainder = key
    if not isinstance(row_id, UUID):
        raise TypeError("cursor row identifier must be a UUID")

    if kind in {CursorKind.TRANSACTIONS, CursorKind.STATEMENT}:
        if type(ordering_value) is not int or ordering_value < 1:
            raise ValueError("posting sequence must be a positive integer")
        encoded: list[object] = [str(ordering_value), str(row_id)]
    else:
        if not isinstance(ordering_value, datetime):
            raise TypeError("cursor timestamp must be a datetime")
        encoded = [_format_datetime(ordering_value), str(row_id)]
    if kind is CursorKind.STATEMENT:
        sequence = remainder[0]
        if type(sequence) is not int or sequence not in (1, 2):
            raise ValueError("statement cursor sequence must be 1 or 2")
        encoded.append(sequence)
    return encoded


def _decode_key(kind: CursorKind, value: object) -> CursorKey:
    expected_parts = 3 if kind is CursorKind.STATEMENT else 2
    if not isinstance(value, list) or len(value) != expected_parts:
        raise _malformed_cursor()

    raw_ordering_value, raw_uuid, *remainder = value
    row_id = _parse_uuid(raw_uuid)

    if kind in {CursorKind.TRANSACTIONS, CursorKind.STATEMENT}:
        ordering_value: datetime | int = _parse_posting_sequence(raw_ordering_value)
    else:
        ordering_value = _parse_datetime(raw_ordering_value)

    if kind is not CursorKind.STATEMENT:
        return (ordering_value, row_id)  # type: ignore[return-value]

    sequence = remainder[0]
    if type(sequence) is not int or sequence not in (1, 2):
        raise _malformed_cursor()
    return (ordering_value, row_id, sequence)  # type: ignore[return-value]


def _parse_posting_sequence(value: object) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise _malformed_cursor()
    if value.startswith("0"):
        raise _malformed_cursor()
    parsed = int(value)
    if parsed < 1 or parsed > 9_223_372_036_854_775_807:
        raise _malformed_cursor()
    return parsed


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cursor datetimes must be timezone-aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise _malformed_cursor()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        canonical = _format_datetime(parsed)
    except (TypeError, ValueError) as exc:
        raise _malformed_cursor() from exc
    if value != canonical:
        raise _malformed_cursor()
    return parsed.astimezone(UTC)


def _parse_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise _malformed_cursor()
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise _malformed_cursor() from exc
    if str(parsed) != value:
        raise _malformed_cursor()
    return parsed


def _filter_fingerprint(filters: Mapping[str, object]) -> str:
    if not isinstance(filters, Mapping):
        raise TypeError("cursor filters must be a mapping")
    normalized = _normalize_filter_value(filters)
    canonical = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _base64url_encode(hashlib.sha256(canonical).digest())


def _normalize_filter_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) is int:
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _format_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("cursor filters cannot contain non-finite decimals")
        return str(value)
    if isinstance(value, Enum):
        return _normalize_filter_value(value.value)
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("cursor filter names must be strings")
            normalized[key] = _normalize_filter_value(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_filter_value(item) for item in value]
    raise TypeError(f"unsupported cursor filter value: {type(value).__name__}")


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _malformed_cursor() -> LedgerError:
    return LedgerError(422, "cursor is malformed")


__all__ = [
    "CURSOR_VERSION",
    "MAX_CURSOR_LENGTH",
    "CursorKey",
    "CursorKind",
    "DecodedCursor",
    "StatementCursorKey",
    "TimestampCursorKey",
    "TransactionCursorKey",
    "TwoPartCursorKey",
    "decode_cursor",
    "encode_cursor",
]
