"""Create the idempotent AED sandbox shown by the integrity console."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from app import services
from app.database import engine
from app.ledger import reverse_transaction
from app.models import (
    Account,
    LedgerTransaction,
    ReconciliationItem,
    ReconciliationRun,
)

TREASURY_ACCOUNT_ID = UUID("11000000-0000-4000-8000-000000000001")
MARKETPLACE_ACCOUNT_ID = UUID("11000000-0000-4000-8000-000000000002")
PAYROLL_ACCOUNT_ID = UUID("11000000-0000-4000-8000-000000000003")
SUPPLIER_ACCOUNT_ID = UUID("11000000-0000-4000-8000-000000000004")

ACCOUNT_FIXTURES = (
    (TREASURY_ACCOUNT_ID, "Operations Treasury"),
    (MARKETPLACE_ACCOUNT_ID, "Dubai Marketplace"),
    (PAYROLL_ACCOUNT_ID, "Payroll Reserve"),
    (SUPPLIER_ACCOUNT_ID, "Supplier Settlements"),
)

DEPOSIT_FIXTURES = (
    (
        TREASURY_ACCOUNT_ID,
        Decimal("250000.00"),
        UUID("21000000-0000-4000-8000-000000000001"),
        (
            UUID("31000000-0000-4000-8000-000000000001"),
            UUID("31000000-0000-4000-8000-000000000002"),
        ),
    ),
    (
        MARKETPLACE_ACCOUNT_ID,
        Decimal("75000.00"),
        UUID("21000000-0000-4000-8000-000000000002"),
        (
            UUID("31000000-0000-4000-8000-000000000003"),
            UUID("31000000-0000-4000-8000-000000000004"),
        ),
    ),
    (
        PAYROLL_ACCOUNT_ID,
        Decimal("150000.00"),
        UUID("21000000-0000-4000-8000-000000000003"),
        (
            UUID("31000000-0000-4000-8000-000000000005"),
            UUID("31000000-0000-4000-8000-000000000006"),
        ),
    ),
    (
        SUPPLIER_ACCOUNT_ID,
        Decimal("25000.00"),
        UUID("21000000-0000-4000-8000-000000000004"),
        (
            UUID("31000000-0000-4000-8000-000000000007"),
            UUID("31000000-0000-4000-8000-000000000008"),
        ),
    ),
)

TRANSFER_FIXTURES = (
    (
        "seed-aed-marketplace-settlement",
        TREASURY_ACCOUNT_ID,
        MARKETPLACE_ACCOUNT_ID,
        Decimal("12450.00"),
    ),
    (
        "seed-aed-supplier-batch",
        TREASURY_ACCOUNT_ID,
        SUPPLIER_ACCOUNT_ID,
        Decimal("8750.00"),
    ),
    (
        "seed-aed-payroll-disbursement",
        PAYROLL_ACCOUNT_ID,
        MARKETPLACE_ACCOUNT_ID,
        Decimal("4200.00"),
    ),
    (
        "seed-aed-marketplace-fee",
        MARKETPLACE_ACCOUNT_ID,
        SUPPLIER_ACCOUNT_ID,
        Decimal("1575.00"),
    ),
    (
        "seed-aed-corrected-supplier-payment",
        TREASURY_ACCOUNT_ID,
        SUPPLIER_ACCOUNT_ID,
        Decimal("3000.00"),
    ),
)

SEED_CREATED_AT = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
SEED_LOCK_ID = 0x4C45444745524C49
RECONCILIATION_RUN_ID = UUID("41000000-0000-4000-8000-000000000001")
UNKNOWN_PROVIDER_TRANSACTION_ID = UUID("51000000-0000-4000-8000-000000000001")


def _create_accounts_and_deposits(session: Session) -> None:
    """Create all opening balances atomically under the seed lock."""

    with session.begin():
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": SEED_LOCK_ID},
        )
        account_insert = postgresql_insert(Account).values(
            [
                {
                    "id": account_id,
                    "currency": "AED",
                    "is_system": False,
                    "system_key": None,
                    "display_name": display_name,
                    "created_at": SEED_CREATED_AT,
                }
                for account_id, display_name in ACCOUNT_FIXTURES
            ]
        )
        session.execute(
            account_insert.on_conflict_do_update(
                index_elements=[Account.id],
                set_={"display_name": account_insert.excluded.display_name},
                where=Account.display_name.is_(None),
            )
        )

        for account_id, display_name in ACCOUNT_FIXTURES:
            account = session.get(Account, account_id)
            if (
                account is None
                or account.currency != "AED"
                or account.is_system
                or account.system_key is not None
                or account.display_name != display_name
                or account.created_at != SEED_CREATED_AT
            ):
                raise RuntimeError(f"seed account {account_id} is inconsistent")

        for offset, fixture in enumerate(DEPOSIT_FIXTURES):
            account_id, amount, transaction_id, entry_ids = fixture
            services._deposit_in_transaction(
                session,
                account_id,
                amount,
                idempotency_key=None,
                transaction_id=transaction_id,
                entry_ids=entry_ids,
                created_at=SEED_CREATED_AT + timedelta(minutes=offset),
            )


def _transaction_for_key(session: Session, key: str) -> LedgerTransaction | None:
    with session.begin():
        return session.scalar(
            select(LedgerTransaction).where(LedgerTransaction.idempotency_key == key)
        )


def _ensure_transfer(
    session: Session,
    key: str,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount: Decimal,
) -> LedgerTransaction:
    existing = _transaction_for_key(session, key)
    if existing is None:
        result = services.transfer(
            session,
            source_account_id,
            destination_account_id,
            amount,
            key,
        )
        transaction_id = UUID(str(result.payload["transaction_id"]))
        with session.begin():
            existing = session.get(LedgerTransaction, transaction_id)

    if (
        existing is None
        or existing.type != "transfer"
        or existing.source_account_id != source_account_id
        or existing.destination_account_id != destination_account_id
        or existing.amount != amount
        or existing.currency != "AED"
    ):
        raise RuntimeError(f"seed transfer {key!r} is inconsistent")
    return existing


def _ensure_reversal(
    session: Session, original: LedgerTransaction
) -> LedgerTransaction:
    key = "seed-aed-corrected-supplier-payment-reversal"
    existing = _transaction_for_key(session, key)
    if existing is None:
        result = reverse_transaction(
            session,
            original.id,
            key,
            "operator_correction",
            note="Duplicate supplier batch detected in sandbox reconciliation",
        )
        transaction_id = UUID(str(result.payload["transaction_id"]))
        with session.begin():
            existing = session.get(LedgerTransaction, transaction_id)

    if (
        existing is None
        or existing.type != "reversal"
        or existing.reverses_transaction_id != original.id
        or existing.amount != original.amount
        or existing.currency != "AED"
    ):
        raise RuntimeError("seed reversal is inconsistent")
    return existing


def _provider_item_values(
    transactions: list[LedgerTransaction],
    reversal: LedgerTransaction,
    created_at: datetime,
) -> list[dict[str, Any]]:
    first, second, third, fourth, corrected = transactions
    fixtures = (
        (first.id, first.amount, "AED", first.created_at),
        (second.id, second.amount, "AED", second.created_at),
        (third.id, third.amount + Decimal("125.00"), "AED", third.created_at),
        (
            UNKNOWN_PROVIDER_TRANSACTION_ID,
            Decimal("615.00"),
            "AED",
            third.created_at + timedelta(microseconds=1),
        ),
        (first.id, first.amount, "AED", first.created_at + timedelta(microseconds=2)),
        (fourth.id, fourth.amount, "USD", fourth.created_at),
        (corrected.id, corrected.amount, "AED", corrected.created_at),
        (reversal.id, reversal.amount, "AED", reversal.created_at),
    )
    return [
        {
            "id": UUID(f"42000000-0000-4000-8000-{index:012d}"),
            "run_id": RECONCILIATION_RUN_ID,
            "provider_reference": f"GULFPAY-AED-2026-{index:04d}",
            "claimed_transaction_id": transaction_id,
            "matched_transaction_id": None,
            "amount": amount,
            "currency": currency,
            "occurred_at": occurred_at,
            "result": "pending",
            "mismatch_code": None,
            "resolution_status": "open",
            "resolution_note": None,
            "created_at": created_at,
            "resolved_at": None,
        }
        for index, (transaction_id, amount, currency, occurred_at) in enumerate(
            fixtures, start=1
        )
    ]


def _ensure_reconciliation_fixture(
    session: Session,
    transactions: list[LedgerTransaction],
    reversal: LedgerTransaction,
) -> ReconciliationRun:
    ledger_timestamps = [transaction.created_at for transaction in transactions]
    ledger_timestamps.append(reversal.created_at)
    created_at = max(ledger_timestamps) + timedelta(minutes=1)
    provider_items = _provider_item_values(transactions, reversal, created_at)
    all_timestamps = ledger_timestamps + [
        item["occurred_at"] for item in provider_items
    ]
    # The settlement window is closed before the run exists. Later transactions
    # therefore cannot drift a completed fixture's evidence.
    period_start = min(all_timestamps) - timedelta(microseconds=1)
    period_end = max(all_timestamps) + timedelta(microseconds=1)

    with session.begin():
        session.execute(
            postgresql_insert(ReconciliationRun)
            .values(
                id=RECONCILIATION_RUN_ID,
                provider="GulfPay Sandbox",
                fixture_key="gulfpay-aed-2026-08",
                currency="AED",
                period_start=period_start,
                period_end=period_end,
                status="pending",
                created_at=created_at,
                completed_at=None,
            )
            .on_conflict_do_nothing(index_elements=[ReconciliationRun.id])
        )
        session.execute(
            postgresql_insert(ReconciliationItem)
            .values(provider_items)
            .on_conflict_do_nothing(index_elements=[ReconciliationItem.id])
        )
        run = session.get(ReconciliationRun, RECONCILIATION_RUN_ID)
        if (
            run is None
            or run.provider != "GulfPay Sandbox"
            or run.fixture_key != "gulfpay-aed-2026-08"
            or run.currency != "AED"
            or run.period_start != period_start
            or run.period_end != period_end
        ):
            raise RuntimeError("seed reconciliation run is inconsistent")
        return run


def _sandbox_balances(session: Session) -> dict[str, str]:
    balances: dict[str, str] = {}
    with session.begin():
        for account_id, display_name in ACCOUNT_FIXTURES:
            statement = services.get_statement(session, account_id)
            balances[display_name] = str(statement["balance"])
    return balances


def seed() -> dict[str, object]:
    """Build the local seed dataset once and return stable summary metadata."""

    # Holding a dedicated connection makes this session-level lock cover every
    # phase and prevents concurrent seed workers from manufacturing replay events.
    with (
        engine.connect() as connection,
        Session(
            bind=connection,
            autoflush=False,
            expire_on_commit=False,
        ) as session,
    ):
        session.execute(
            text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": SEED_LOCK_ID}
        )
        session.commit()
        try:
            _create_accounts_and_deposits(session)
            transactions = [
                _ensure_transfer(session, key, source, destination, amount)
                for key, source, destination, amount in TRANSFER_FIXTURES
            ]
            reversal = _ensure_reversal(session, transactions[-1])
            run = _ensure_reconciliation_fixture(session, transactions, reversal)
            balances = _sandbox_balances(session)
            result: dict[str, object] = {
                "currency": "AED",
                "customer_accounts": len(ACCOUNT_FIXTURES),
                "transactions": len(DEPOSIT_FIXTURES) + len(transactions) + 1,
                "reconciliation_run_id": str(run.id),
                "balances": balances,
            }
        finally:
            session.rollback()
            session.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": SEED_LOCK_ID},
            )
            session.commit()
    return result


def main() -> None:
    print(json.dumps(seed(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
