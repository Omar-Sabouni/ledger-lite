from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import services
from app.database import SessionLocal
from app.main import app
from app.models import (
    Account,
    LedgerEntry,
    LedgerTransaction,
    OutboxEvent,
    ReconciliationItem,
    ReconciliationRun,
)

pytestmark = pytest.mark.integration


def create_account(client: TestClient, currency: str = "USD") -> dict[str, Any]:
    response = client.post("/accounts", json={"currency": currency})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["currency"] == currency
    assert body["balance"] == "0.00"
    return body


def post_deposit(
    client: TestClient,
    account_id: str,
    amount: object,
    idempotency_key: str,
) -> Response:
    return client.post(
        f"/accounts/{account_id}/deposits",
        headers={"Idempotency-Key": idempotency_key},
        json={"amount": amount},
    )


def deposit(
    client: TestClient,
    account_id: str,
    amount: str,
    idempotency_key: str,
) -> dict[str, Any]:
    response = post_deposit(client, account_id, amount, idempotency_key)
    assert response.status_code == 201, response.text
    assert response.headers["Idempotent-Replayed"].lower() == "false"
    return response.json()


def transfer(
    client: TestClient,
    source_account_id: str,
    destination_account_id: str,
    amount: object,
    idempotency_key: str,
):
    return client.post(
        "/transfers",
        headers={"Idempotency-Key": idempotency_key},
        json={
            "source_account_id": source_account_id,
            "destination_account_id": destination_account_id,
            "amount": amount,
        },
    )


def statement(client: TestClient, account_id: str) -> dict[str, Any]:
    response = client.get(f"/accounts/{account_id}/statement")
    assert response.status_code == 200, response.text
    return response.json()


def test_health_reports_database_readiness(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}


def test_console_capabilities_expose_optional_links(client: TestClient) -> None:
    from app.main import settings

    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200, response.text
    assert response.json() == {"documentation": settings.app_env != "production"}


def test_api_unknown_paths_preserve_method_not_allowed(client: TestClient) -> None:
    wrong_method = client.put("/api/v1/accounts")
    missing_path = client.delete("/api/v1/definitely-missing")

    assert wrong_method.status_code == 405
    assert set(wrong_method.headers["Allow"].split(", ")) == {"GET", "POST"}
    assert wrong_method.json()["code"] == "method_not_allowed"
    assert missing_path.status_code == 404
    assert missing_path.json()["code"] == "not_found"


def test_duplicate_deposit_replays_without_moving_money_twice(
    client: TestClient, db_session: Session
) -> None:
    account = create_account(client)

    first = post_deposit(client, account["id"], "25.00", "duplicate-deposit-001")
    replay = post_deposit(client, account["id"], "25.00", "duplicate-deposit-001")

    assert first.status_code == 201, first.text
    assert replay.status_code == first.status_code, replay.text
    assert first.headers["Idempotent-Replayed"].lower() == "false"
    assert replay.headers["Idempotent-Replayed"].lower() == "true"
    assert replay.json() == first.json()
    assert statement(client, account["id"])["balance"] == "25.00"

    transaction_id = UUID(first.json()["transaction_id"])
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(LedgerTransaction)
            .where(LedgerTransaction.id == transaction_id)
        )
        == 1
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.transaction_id == transaction_id)
        )
        == 2
    )


def test_reusing_deposit_key_with_different_payload_is_a_conflict(
    client: TestClient,
) -> None:
    account = create_account(client)

    first = post_deposit(client, account["id"], "25.00", "conflicting-deposit-001")
    conflict = post_deposit(client, account["id"], "26.00", "conflicting-deposit-001")

    assert first.status_code == 201, first.text
    assert first.headers["Idempotent-Replayed"].lower() == "false"
    assert conflict.status_code == 409, conflict.text
    assert statement(client, account["id"])["balance"] == "25.00"


def test_deposit_requires_idempotency_key(client: TestClient) -> None:
    account = create_account(client)

    response = client.post(
        f"/accounts/{account['id']}/deposits", json={"amount": "25.00"}
    )

    assert response.status_code == 422, response.text
    assert statement(client, account["id"])["balance"] == "0.00"


def test_every_transaction_balances_and_account_balances_are_calculated(
    client: TestClient, db_session: Session
) -> None:
    source = create_account(client)
    destination = create_account(client)

    posted_deposit = deposit(client, source["id"], "100.00", "balanced-deposit-001")
    assert posted_deposit["amount"] == "100.00"
    assert posted_deposit["balance"] == "100.00"

    posted_transfer = transfer(
        client,
        source["id"],
        destination["id"],
        "35.25",
        "balanced-transfer-001",
    )
    assert posted_transfer.status_code == 201, posted_transfer.text
    assert posted_transfer.json()["amount"] == "35.25"

    assert statement(client, source["id"])["balance"] == "64.75"
    assert statement(client, destination["id"])["balance"] == "35.25"

    transaction_totals = db_session.execute(
        select(
            LedgerEntry.transaction_id,
            func.sum(LedgerEntry.amount),
            func.count(LedgerEntry.id),
        ).group_by(LedgerEntry.transaction_id)
    ).all()

    assert len(transaction_totals) == 2
    for _, total, entry_count in transaction_totals:
        assert total == Decimal("0.00")
        assert entry_count == 2


def test_seed_is_deterministic_and_does_not_duplicate_money(
    db_session: Session,
) -> None:
    from app.seed import seed

    first = seed()
    second = seed()

    assert (
        first
        == second
        == {
            "currency": "AED",
            "customer_accounts": 4,
            "transactions": 10,
            "reconciliation_run_id": "41000000-0000-4000-8000-000000000001",
            "balances": {
                "Operations Treasury": "228800.00",
                "Dubai Marketplace": "90075.00",
                "Payroll Reserve": "145800.00",
                "Supplier Settlements": "35325.00",
            },
        }
    )
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(Account)) == 5
    assert db_session.scalar(select(func.count()).select_from(LedgerTransaction)) == 10
    assert db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 20
    assert db_session.scalar(select(func.count()).select_from(OutboxEvent)) == 10
    assert db_session.scalar(select(func.count()).select_from(ReconciliationRun)) == 1
    assert db_session.scalar(select(func.count()).select_from(ReconciliationItem)) == 8


def test_seed_rolls_back_everything_when_the_second_deposit_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.seed import seed

    posted_entry_count = 0

    def fail_during_second_deposit(_posted_entry: LedgerEntry) -> None:
        nonlocal posted_entry_count
        posted_entry_count += 1
        if posted_entry_count == 2:
            raise RuntimeError("injected failure during second seed deposit")

    with monkeypatch.context() as patch:
        patch.setattr(services, "_after_first_entry", fail_during_second_deposit)
        with pytest.raises(
            RuntimeError, match="injected failure during second seed deposit"
        ):
            seed()

    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(Account)) == 0
    assert db_session.scalar(select(func.count()).select_from(LedgerTransaction)) == 0
    assert db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


def test_duplicate_idempotency_key_replays_original_without_moving_money(
    client: TestClient, db_session: Session
) -> None:
    source = create_account(client)
    destination = create_account(client)
    deposit(client, source["id"], "100.00", "duplicate-transfer-funding-001")

    first = transfer(
        client,
        source["id"],
        destination["id"],
        "30.00",
        "duplicate-key-001",
    )
    replay = transfer(
        client,
        source["id"],
        destination["id"],
        "30.00",
        "duplicate-key-001",
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == first.status_code, replay.text
    assert replay.json() == first.json()
    assert first.headers["Idempotent-Replayed"].lower() == "false"
    assert replay.headers["Idempotent-Replayed"].lower() == "true"
    assert statement(client, source["id"])["balance"] == "70.00"
    assert statement(client, destination["id"])["balance"] == "30.00"

    transaction_id = UUID(first.json()["transaction_id"])
    matching_transactions = db_session.scalar(
        select(func.count())
        .select_from(LedgerTransaction)
        .where(LedgerTransaction.id == transaction_id)
    )
    matching_entries = db_session.scalar(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.transaction_id == transaction_id)
    )
    assert matching_transactions == 1
    assert matching_entries == 2


def test_reusing_idempotency_key_with_different_payload_is_a_conflict(
    client: TestClient,
) -> None:
    source = create_account(client)
    destination = create_account(client)
    deposit(client, source["id"], "100.00", "conflicting-transfer-funding-001")

    first = transfer(
        client,
        source["id"],
        destination["id"],
        "30.00",
        "conflicting-key-001",
    )
    conflict = transfer(
        client,
        source["id"],
        destination["id"],
        "31.00",
        "conflicting-key-001",
    )

    assert first.status_code == 201, first.text
    assert conflict.status_code == 409, conflict.text
    assert statement(client, source["id"])["balance"] == "70.00"
    assert statement(client, destination["id"])["balance"] == "30.00"


def test_failure_after_first_entry_rolls_back_the_entire_transfer(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_account(client)
    destination = create_account(client)
    deposit(client, source["id"], "50.00", "rollback-transfer-funding-001")
    transactions_before = db_session.scalar(
        select(func.count()).select_from(LedgerTransaction)
    )

    def fail_after_first_entry(_posted_entry: LedgerEntry) -> None:
        raise RuntimeError("injected failure after the first ledger entry")

    with monkeypatch.context() as patch:
        patch.setattr(services, "_after_first_entry", fail_after_first_entry)
        with TestClient(app, raise_server_exceptions=False) as failure_client:
            failed = transfer(
                failure_client,
                source["id"],
                destination["id"],
                "12.50",
                "rollback-key-001",
            )

    assert failed.status_code == 500, failed.text
    db_session.expire_all()
    assert (
        db_session.scalar(select(func.count()).select_from(LedgerTransaction))
        == transactions_before
    )
    assert statement(client, source["id"])["balance"] == "50.00"
    assert statement(client, destination["id"])["balance"] == "0.00"

    # The idempotency reservation participates in the same rollback, so retrying
    # the exact request after the transient failure can post normally.
    retry = transfer(
        client,
        source["id"],
        destination["id"],
        "12.50",
        "rollback-key-001",
    )
    assert retry.status_code == 201, retry.text
    assert statement(client, source["id"])["balance"] == "37.50"
    assert statement(client, destination["id"])["balance"] == "12.50"


def test_insufficient_funds_rejects_transfer_without_ledger_writes(
    client: TestClient, db_session: Session
) -> None:
    source = create_account(client)
    destination = create_account(client)
    deposit(client, source["id"], "10.00", "insufficient-transfer-funding-001")
    entries_before = db_session.scalar(select(func.count()).select_from(LedgerEntry))

    rejected = transfer(
        client,
        source["id"],
        destination["id"],
        "10.01",
        "insufficient-key-001",
    )

    assert rejected.status_code == 409, rejected.text
    db_session.expire_all()
    assert (
        db_session.scalar(select(func.count()).select_from(LedgerEntry))
        == entries_before
    )
    assert statement(client, source["id"])["balance"] == "10.00"
    assert statement(client, destination["id"])["balance"] == "0.00"


@pytest.mark.parametrize("currency", ["US", "USDX", "usd", "12A"])
def test_account_currency_must_be_three_uppercase_letters(
    client: TestClient, currency: str
) -> None:
    response = client.post("/accounts", json={"currency": currency})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "amount", ["0.00", "-1.00", "1.001", 1.25, "1000000000000000000.00"]
)
def test_deposit_rejects_invalid_money_amounts(
    client: TestClient, amount: object
) -> None:
    account = create_account(client)
    response = post_deposit(client, account["id"], amount, "invalid-deposit-amount-001")
    assert response.status_code == 422, response.text
    assert statement(client, account["id"])["balance"] == "0.00"


def test_transfer_rejects_currency_mismatch_and_self_transfer(
    client: TestClient,
) -> None:
    usd = create_account(client, "USD")
    other_usd = create_account(client, "USD")
    eur = create_account(client, "EUR")
    deposit(client, usd["id"], "20.00", "currency-check-funding-001")

    mismatch = transfer(
        client,
        usd["id"],
        eur["id"],
        "5.00",
        "currency-mismatch-001",
    )
    same_account = transfer(
        client,
        usd["id"],
        usd["id"],
        "5.00",
        "self-transfer-001",
    )

    assert mismatch.status_code == 422, mismatch.text
    assert same_account.status_code == 422, same_account.text
    assert statement(client, usd["id"])["balance"] == "20.00"
    assert statement(client, other_usd["id"])["balance"] == "0.00"
    assert statement(client, eur["id"])["balance"] == "0.00"


def test_transfer_requires_idempotency_key_and_valid_decimal_amount(
    client: TestClient,
) -> None:
    source = create_account(client)
    destination = create_account(client)
    deposit(client, source["id"], "20.00", "validation-funding-001")
    payload = {
        "source_account_id": source["id"],
        "destination_account_id": destination["id"],
        "amount": "5.00",
    }

    missing_key = client.post("/transfers", json=payload)
    assert missing_key.status_code == 422, missing_key.text

    invalid_amounts = (
        "0.00",
        "-1.00",
        "1.001",
        1.25,
        "1000000000000000000.00",
    )
    for index, invalid_amount in enumerate(invalid_amounts):
        invalid = transfer(
            client,
            source["id"],
            destination["id"],
            invalid_amount,
            f"invalid-transfer-amount-{index}",
        )
        assert invalid.status_code == 422, invalid.text

    assert statement(client, source["id"])["balance"] == "20.00"
    assert statement(client, destination["id"])["balance"] == "0.00"


def test_statement_is_newest_first_and_stable(client: TestClient) -> None:
    source = create_account(client)
    destination = create_account(client)

    first = deposit(client, source["id"], "10.00", "statement-deposit-001")
    second = deposit(client, source["id"], "20.00", "statement-deposit-002")
    third = deposit(client, source["id"], "30.00", "statement-deposit-003")
    fourth_response = transfer(
        client,
        source["id"],
        destination["id"],
        "7.00",
        "statement-order-001",
    )
    assert fourth_response.status_code == 201, fourth_response.text
    fourth = fourth_response.json()

    first_statement = statement(client, source["id"])
    second_statement = statement(client, source["id"])
    entries = first_statement["entries"]

    assert first_statement == second_statement
    assert first_statement["balance"] == "53.00"
    assert [entry["transaction_id"] for entry in entries] == [
        fourth["transaction_id"],
        third["transaction_id"],
        second["transaction_id"],
        first["transaction_id"],
    ]
    assert [entry["amount"] for entry in entries] == [
        "-7.00",
        "30.00",
        "20.00",
        "10.00",
    ]
    created_at = [datetime.fromisoformat(entry["created_at"]) for entry in entries]
    assert created_at == sorted(created_at, reverse=True)


def test_cannot_append_a_balanced_pair_to_an_existing_transaction(
    client: TestClient, db_session: Session
) -> None:
    source = create_account(client)
    destination = create_account(client)
    deposit(client, source["id"], "50.00", "append-check-funding-001")
    posted = transfer(
        client,
        source["id"],
        destination["id"],
        "10.00",
        "exactly-two-entries-001",
    )
    assert posted.status_code == 201, posted.text
    transaction_id = UUID(posted.json()["transaction_id"])

    append_session = SessionLocal()
    try:
        append_session.add_all(
            [
                LedgerEntry(
                    transaction_id=transaction_id,
                    account_id=UUID(source["id"]),
                    sequence=3,
                    amount=Decimal("-1.00"),
                    currency="USD",
                ),
                LedgerEntry(
                    transaction_id=transaction_id,
                    account_id=UUID(destination["id"]),
                    sequence=4,
                    amount=Decimal("1.00"),
                    currency="USD",
                ),
            ]
        )

        # Sequences are limited to the two declared postings, so a third/fourth
        # balanced pair is rejected before it can reach the deferred assertion.
        with pytest.raises(IntegrityError):
            append_session.flush()
        append_session.rollback()
    finally:
        append_session.close()

    db_session.expire_all()
    entry_count = db_session.scalar(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.transaction_id == transaction_id)
    )
    entry_total = db_session.scalar(
        select(func.sum(LedgerEntry.amount)).where(
            LedgerEntry.transaction_id == transaction_id
        )
    )
    assert entry_count == 2
    assert entry_total == Decimal("0.00")


def _assert_semantically_invalid_transfer_is_rejected(
    db_session: Session,
    *,
    declared_source_id: UUID,
    declared_destination_id: UUID,
    source_entry_account_id: UUID,
    posted_amount: Decimal,
    declared_amount: Decimal,
) -> None:
    transaction_id = uuid4()
    created_at = datetime.now(UTC)
    invalid_session = SessionLocal()
    try:
        invalid_session.add_all(
            [
                LedgerTransaction(
                    id=transaction_id,
                    type="transfer",
                    amount=declared_amount,
                    currency="USD",
                    source_account_id=declared_source_id,
                    destination_account_id=declared_destination_id,
                    idempotency_key=f"invalid-semantic-{transaction_id}",
                    request_fingerprint="0" * 64,
                    response_payload={},
                    created_at=created_at,
                ),
                LedgerEntry(
                    transaction_id=transaction_id,
                    account_id=source_entry_account_id,
                    sequence=1,
                    amount=-posted_amount,
                    currency="USD",
                    created_at=created_at,
                ),
                LedgerEntry(
                    transaction_id=transaction_id,
                    account_id=declared_destination_id,
                    sequence=2,
                    amount=posted_amount,
                    currency="USD",
                    created_at=created_at,
                ),
            ]
        )

        # The rows are zero-sum and currency-consistent; only the declared
        # transaction semantics are wrong, and the deferred DB check rejects it.
        with pytest.raises(IntegrityError):
            invalid_session.commit()
        invalid_session.rollback()
    finally:
        invalid_session.close()

    db_session.expire_all()
    assert db_session.get(LedgerTransaction, transaction_id) is None


def test_database_rejects_entries_that_disagree_with_declared_amount(
    client: TestClient, db_session: Session
) -> None:
    source = create_account(client)
    destination = create_account(client)

    _assert_semantically_invalid_transfer_is_rejected(
        db_session,
        declared_source_id=UUID(source["id"]),
        declared_destination_id=UUID(destination["id"]),
        source_entry_account_id=UUID(source["id"]),
        posted_amount=Decimal("9.00"),
        declared_amount=Decimal("10.00"),
    )


def test_database_rejects_entries_posted_to_an_undeclared_account(
    client: TestClient, db_session: Session
) -> None:
    source = create_account(client)
    destination = create_account(client)
    undeclared = create_account(client)

    _assert_semantically_invalid_transfer_is_rejected(
        db_session,
        declared_source_id=UUID(source["id"]),
        declared_destination_id=UUID(destination["id"]),
        source_entry_account_id=UUID(undeclared["id"]),
        posted_amount=Decimal("10.00"),
        declared_amount=Decimal("10.00"),
    )


def test_account_role_is_immutable(client: TestClient, db_session: Session) -> None:
    account = create_account(client)
    account_id = UUID(account["id"])
    mutation_session = SessionLocal()
    try:
        # Changing both fields would remain a structurally valid clearing role;
        # the identity trigger, rather than a shape CHECK, must still reject it.
        with pytest.raises(IntegrityError):
            mutation_session.execute(
                update(Account)
                .where(Account.id == account_id)
                .values(is_system=True, system_key="clearing:USD")
            )
            mutation_session.commit()
        mutation_session.rollback()
    finally:
        mutation_session.close()

    db_session.expire_all()
    stored = db_session.get(Account, account_id)
    assert stored is not None
    assert stored.is_system is False
    assert stored.system_key is None


def test_posted_ledger_rows_reject_updates_and_deletes(
    client: TestClient, db_session: Session
) -> None:
    account = create_account(client)
    posted = deposit(client, account["id"], "50.00", "immutability-deposit-001")
    transaction_id = UUID(posted["transaction_id"])
    entry_id = db_session.scalar(
        select(LedgerEntry.id).where(
            LedgerEntry.transaction_id == transaction_id,
            LedgerEntry.account_id == UUID(account["id"]),
        )
    )
    assert entry_id is not None

    mutation_session = SessionLocal()
    try:
        with pytest.raises(IntegrityError):
            mutation_session.execute(
                update(LedgerEntry)
                .where(LedgerEntry.id == entry_id)
                .values(amount=Decimal("49.00"))
            )
        mutation_session.rollback()

        with pytest.raises(IntegrityError):
            mutation_session.execute(
                delete(LedgerTransaction).where(LedgerTransaction.id == transaction_id)
            )
        mutation_session.rollback()
    finally:
        mutation_session.close()

    db_session.expire_all()
    assert db_session.get(LedgerTransaction, transaction_id) is not None
    assert db_session.get(LedgerEntry, entry_id).amount == Decimal("50.00")


def test_concurrent_duplicate_requests_post_one_transfer_and_replay_it(
    client: TestClient, db_session: Session
) -> None:
    source = create_account(client)
    destination = create_account(client)
    deposit(client, source["id"], "100.00", "concurrent-replay-funding-001")
    start_together = Barrier(3)

    def post_transfer() -> tuple[int, dict[str, Any], str | None]:
        with TestClient(app) as concurrent_client:
            start_together.wait(timeout=10)
            response = transfer(
                concurrent_client,
                source["id"],
                destination["id"],
                "30.00",
                "concurrent-duplicate-001",
            )
            return (
                response.status_code,
                response.json(),
                response.headers.get("Idempotent-Replayed"),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(post_transfer)
        second_future = pool.submit(post_transfer)
        start_together.wait(timeout=10)
        results = [
            first_future.result(timeout=20),
            second_future.result(timeout=20),
        ]

    assert [status for status, _, _ in results] == [201, 201]
    assert results[0][1] == results[1][1]
    assert len({result[1]["transaction_id"] for result in results}) == 1
    assert sorted(replayed for _, _, replayed in results) == ["false", "true"]

    transaction_id = UUID(results[0][1]["transaction_id"])
    db_session.expire_all()
    transfer_count = db_session.scalar(
        select(func.count())
        .select_from(LedgerTransaction)
        .where(LedgerTransaction.idempotency_key == "concurrent-duplicate-001")
    )
    entry_count = db_session.scalar(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.transaction_id == transaction_id)
    )
    assert transfer_count == 1
    assert entry_count == 2
    assert statement(client, source["id"])["balance"] == "70.00"
    assert statement(client, destination["id"])["balance"] == "30.00"


def test_concurrent_withdrawals_cannot_overdraw_source_account(
    client: TestClient, db_session: Session
) -> None:
    source = create_account(client)
    first_destination = create_account(client)
    second_destination = create_account(client)
    deposit(client, source["id"], "100.00", "concurrent-withdrawal-funding-001")
    start_together = Barrier(3)

    def withdraw(destination_id: str, key: str) -> tuple[int, dict[str, Any]]:
        with TestClient(app) as concurrent_client:
            start_together.wait(timeout=10)
            response = transfer(
                concurrent_client,
                source["id"],
                destination_id,
                "75.00",
                key,
            )
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(
            withdraw, first_destination["id"], "concurrent-withdrawal-001"
        )
        second_future = pool.submit(
            withdraw, second_destination["id"], "concurrent-withdrawal-002"
        )
        start_together.wait(timeout=10)
        results = [
            first_future.result(timeout=20),
            second_future.result(timeout=20),
        ]

    assert sorted(status for status, _ in results) == [201, 409]
    failed_payload = next(payload for status, payload in results if status == 409)
    assert failed_payload["detail"] == "insufficient funds"

    db_session.expire_all()
    posted_transfers = db_session.scalar(
        select(func.count())
        .select_from(LedgerTransaction)
        .where(LedgerTransaction.type == "transfer")
    )
    source_statement = statement(client, source["id"])
    destination_balances = sorted(
        [
            Decimal(statement(client, first_destination["id"])["balance"]),
            Decimal(statement(client, second_destination["id"])["balance"]),
        ]
    )
    assert posted_transfers == 1
    assert Decimal(source_statement["balance"]) == Decimal("25.00")
    assert Decimal(source_statement["balance"]) >= Decimal("0.00")
    assert destination_balances == [Decimal("0.00"), Decimal("75.00")]
