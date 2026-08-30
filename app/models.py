from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_accounts_currency_code"),
        CheckConstraint(
            "(is_system AND system_key IS NOT NULL "
            "AND system_key = 'clearing:' || currency) "
            "OR (NOT is_system AND system_key IS NULL)",
            name="ck_accounts_system_identity",
        ),
        CheckConstraint(
            "display_name IS NULL OR "
            "(display_name = btrim(display_name) AND length(display_name) > 0)",
            name="ck_accounts_display_name",
        ),
        UniqueConstraint("system_key", name="uq_accounts_system_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    system_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    entries: Mapped[list[LedgerEntry]] = relationship(
        back_populates="account", lazy="raise"
    )


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_ledger_transactions_amount_positive"),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_ledger_transactions_currency_code",
        ),
        CheckConstraint(
            "type IN ('deposit', 'transfer', 'reversal')",
            name="ck_ledger_transactions_type",
        ),
        CheckConstraint(
            "source_account_id <> destination_account_id",
            name="ck_ledger_transactions_distinct_accounts",
        ),
        CheckConstraint(
            "(type = 'deposit' AND "
            "((idempotency_key IS NULL AND request_fingerprint IS NULL) OR "
            "(idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL))) "
            "OR (type IN ('transfer', 'reversal') "
            "AND idempotency_key IS NOT NULL "
            "AND request_fingerprint IS NOT NULL)",
            name="ck_ledger_transactions_idempotency_pair",
        ),
        CheckConstraint(
            "request_fingerprint IS NULL OR request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_ledger_transactions_request_fingerprint_shape",
        ),
        CheckConstraint(
            "(type = 'reversal' "
            "AND reverses_transaction_id IS NOT NULL "
            "AND reversal_reason_code IS NOT NULL) "
            "OR (type <> 'reversal' "
            "AND reverses_transaction_id IS NULL "
            "AND reversal_reason_code IS NULL "
            "AND reversal_note IS NULL)",
            name="ck_ledger_transactions_reversal_fields",
        ),
        CheckConstraint(
            "reversal_reason_code IS NULL OR reversal_reason_code IN "
            "('duplicate', 'customer_request', 'operator_correction', 'other')",
            name="ck_ledger_transactions_reversal_reason",
        ),
        CheckConstraint(
            "reversal_note IS NULL OR "
            "(reversal_note = btrim(reversal_note) AND length(reversal_note) > 0)",
            name="ck_ledger_transactions_reversal_note",
        ),
        CheckConstraint(
            "reverses_transaction_id IS NULL OR reverses_transaction_id <> id",
            name="ck_ledger_transactions_not_self_reversal",
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_ledger_transactions_idempotency_key"
        ),
        UniqueConstraint(
            "reverses_transaction_id",
            name="uq_ledger_transactions_reverses_transaction_id",
        ),
        UniqueConstraint(
            "posting_sequence",
            name="uq_ledger_transactions_posting_sequence",
        ),
        Index(
            "ix_ledger_transactions_created",
            "created_at",
            "id",
        ),
        Index(
            "ix_ledger_transactions_currency_type_created",
            "currency",
            "type",
            "created_at",
            "id",
        ),
        Index(
            "ix_ledger_transactions_type_created",
            "type",
            "created_at",
            "id",
        ),
        Index(
            "ix_ledger_transactions_source_created",
            "source_account_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_ledger_transactions_destination_created",
            "destination_account_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    destination_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reverses_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=True
    )
    reversal_reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reversal_note: Mapped[str | None] = mapped_column(String(240), nullable=True)
    # A SECURITY DEFINER trigger assigns this under the commit-order advisory
    # lock. Marking it server-generated keeps the restricted runtime role from
    # naming a column it is intentionally not allowed to INSERT.
    posting_sequence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=FetchedValue(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    entries: Mapped[list[LedgerEntry]] = relationship(
        back_populates="transaction", lazy="raise"
    )
    reverses: Mapped[LedgerTransaction | None] = relationship(
        back_populates="reversal",
        foreign_keys=[reverses_transaction_id],
        lazy="raise",
        remote_side=[id],
    )
    reversal: Mapped[LedgerTransaction | None] = relationship(
        back_populates="reverses",
        foreign_keys=[reverses_transaction_id],
        lazy="raise",
        uselist=False,
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_ledger_entries_amount_nonzero"),
        CheckConstraint("sequence IN (1, 2)", name="ck_ledger_entries_sequence"),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name="ck_ledger_entries_currency_code"
        ),
        UniqueConstraint(
            "transaction_id",
            "sequence",
            name="uq_ledger_entries_transaction_sequence",
        ),
        Index(
            "ix_ledger_entries_account_statement",
            "account_id",
            "created_at",
            "transaction_id",
            "sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    transaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    account: Mapped[Account] = relationship(back_populates="entries", lazy="raise")
    transaction: Mapped[LedgerTransaction] = relationship(
        back_populates="entries", lazy="raise"
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN "
            "('posting.created', 'reversal.created', 'request.replayed', "
            "'reconciliation.completed', 'reconciliation.resolved')",
            name="ck_outbox_events_event_type",
        ),
        CheckConstraint(
            "aggregate_type IN "
            "('ledger_transaction', 'reconciliation_run', 'reconciliation_item')",
            name="ck_outbox_events_aggregate_type",
        ),
        CheckConstraint(
            "request_id IS NULL OR "
            "(request_id = btrim(request_id) AND length(request_id) > 0)",
            name="ck_outbox_events_request_id",
        ),
        CheckConstraint(
            "json_typeof(payload) = 'object'", name="ck_outbox_events_payload_object"
        ),
        CheckConstraint(
            "octet_length(payload::text) <= 4096",
            name="ck_outbox_events_payload_size",
        ),
        CheckConstraint("id > 0", name="ck_outbox_events_id_positive"),
        Index(
            "ix_outbox_events_aggregate",
            "aggregate_type",
            "aggregate_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        autoincrement=False,
        server_default=FetchedValue(),
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_reconciliation_runs_currency_code",
        ),
        CheckConstraint(
            "period_start < period_end", name="ck_reconciliation_runs_period"
        ),
        CheckConstraint(
            "status IN ('pending', 'completed')",
            name="ck_reconciliation_runs_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL AND summary IS NULL) "
            "OR (status = 'completed' AND completed_at IS NOT NULL "
            "AND summary IS NOT NULL)",
            name="ck_reconciliation_runs_completion",
        ),
        CheckConstraint(
            "summary IS NULL OR json_typeof(summary) = 'object'",
            name="ck_reconciliation_runs_summary_object",
        ),
        UniqueConstraint("fixture_key", name="uq_reconciliation_runs_fixture_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    fixture_key: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    items: Mapped[list[ReconciliationItem]] = relationship(
        back_populates="run", lazy="raise"
    )


class ReconciliationItem(Base):
    __tablename__ = "reconciliation_items"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_reconciliation_items_amount_positive"),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_reconciliation_items_currency_code",
        ),
        CheckConstraint(
            "result IN "
            "('pending', 'matched', 'provider_only', 'ledger_only', "
            "'mismatched', 'duplicate')",
            name="ck_reconciliation_items_result",
        ),
        CheckConstraint(
            "mismatch_code IS NULL OR mismatch_code IN "
            "('transaction_not_found', 'amount_mismatch', 'currency_mismatch', "
            "'transaction_type_mismatch', 'outside_period', 'duplicate_claim', "
            "'unclaimed_ledger_transaction')",
            name="ck_reconciliation_items_mismatch_code",
        ),
        CheckConstraint(
            "resolution_status IN ('open', 'matched', 'ignored')",
            name="ck_reconciliation_items_resolution_status",
        ),
        CheckConstraint(
            "(result IN ('pending', 'matched') AND mismatch_code IS NULL) OR "
            "(result = 'provider_only' "
            "AND mismatch_code = 'transaction_not_found') OR "
            "(result = 'ledger_only' "
            "AND mismatch_code = 'unclaimed_ledger_transaction') OR "
            "(result = 'mismatched' AND mismatch_code IN "
            "('amount_mismatch', 'currency_mismatch', "
            "'transaction_type_mismatch', 'outside_period')) OR "
            "(result = 'duplicate' AND mismatch_code = 'duplicate_claim')",
            name="ck_reconciliation_items_result_mismatch",
        ),
        CheckConstraint(
            "(result = 'ledger_only' AND provider_reference IS NULL) OR "
            "(result <> 'ledger_only' AND provider_reference IS NOT NULL)",
            name="ck_reconciliation_items_provider_reference",
        ),
        CheckConstraint(
            "resolution_note IS NULL OR "
            "(resolution_note = btrim(resolution_note) "
            "AND length(resolution_note) > 0)",
            name="ck_reconciliation_items_resolution_note",
        ),
        CheckConstraint(
            "resolution_status <> 'ignored' OR resolution_note IS NOT NULL",
            name="ck_reconciliation_items_ignore_reason",
        ),
        CheckConstraint(
            "(resolution_status = 'open' AND resolved_at IS NULL) OR "
            "(resolution_status <> 'open' AND resolved_at IS NOT NULL)",
            name="ck_reconciliation_items_resolution_time",
        ),
        CheckConstraint(
            "(result = 'matched' AND resolution_status = 'matched' "
            "AND matched_transaction_id IS NOT NULL) OR "
            "(result <> 'matched' AND matched_transaction_id IS NULL)",
            name="ck_reconciliation_items_match_evidence",
        ),
        CheckConstraint(
            "resolution_status <> 'matched' OR result IN ('matched', 'ledger_only')",
            name="ck_reconciliation_items_matched_state",
        ),
        CheckConstraint(
            "resolution_status <> 'ignored' OR result NOT IN ('pending', 'matched')",
            name="ck_reconciliation_items_ignored_state",
        ),
        Index(
            "ix_reconciliation_items_run_result",
            "run_id",
            "result",
            "created_at",
            "id",
        ),
        Index(
            "ix_reconciliation_items_run_resolution",
            "run_id",
            "resolution_status",
            "created_at",
            "id",
        ),
        Index(
            "uq_reconciliation_items_run_provider_reference",
            "run_id",
            "provider_reference",
            unique=True,
            postgresql_where=text("provider_reference IS NOT NULL"),
        ),
        Index(
            "uq_reconciliation_items_run_matched_transaction",
            "run_id",
            "matched_transaction_id",
            unique=True,
            postgresql_where=text("matched_transaction_id IS NOT NULL"),
        ),
        Index(
            "uq_reconciliation_items_run_ledger_transaction",
            "run_id",
            "claimed_transaction_id",
            unique=True,
            postgresql_where=text("result = 'ledger_only'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False
    )
    provider_reference: Mapped[str | None] = mapped_column(String(80), nullable=True)
    claimed_transaction_id: Mapped[UUID | None] = mapped_column(nullable=True)
    matched_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    result: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    mismatch_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resolution_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open"
    )
    resolution_note: Mapped[str | None] = mapped_column(String(240), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    run: Mapped[ReconciliationRun] = relationship(back_populates="items", lazy="raise")
    matched_transaction: Mapped[LedgerTransaction | None] = relationship(lazy="raise")


class IdempotencyResult:
    """Service-layer replay result; deliberately not persisted as its own row."""

    def __init__(self, payload: dict[str, Any], replayed: bool) -> None:
        self.payload = payload
        self.replayed = replayed


__all__ = [
    "Account",
    "Base",
    "IdempotencyResult",
    "LedgerEntry",
    "LedgerTransaction",
    "OutboxEvent",
    "ReconciliationItem",
    "ReconciliationRun",
]
