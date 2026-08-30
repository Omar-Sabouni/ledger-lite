from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.errors import LedgerError
from app.models import (
    Account,
    IdempotencyResult,
    LedgerEntry,
    LedgerTransaction,
    OutboxEvent,
)

CENT = Decimal("0.01")
MAX_OUTBOX_PAYLOAD_BYTES = 4096
ACCOUNT_CREATION_NAMESPACE = UUID("9bcf7f96-193d-4bc2-9015-2d39a581b51b")
REVERSAL_REASON_CODES = frozenset(
    {"duplicate", "customer_request", "operator_correction", "other"}
)
OUTBOX_EVENT_TYPES = frozenset(
    {
        "posting.created",
        "reversal.created",
        "request.replayed",
        "reconciliation.completed",
        "reconciliation.resolved",
    }
)
OUTBOX_AGGREGATE_TYPES = frozenset(
    {"ledger_transaction", "reconciliation_run", "reconciliation_item"}
)


def _format_money(amount: Decimal) -> str:
    return format(amount.quantize(CENT), ".2f")


def _validate_idempotency_key(key: str) -> None:
    if not isinstance(key, str) or not 1 <= len(key) <= 255:
        raise LedgerError(422, "idempotency key must be 1 to 255 characters")
    if any(not 33 <= ord(char) <= 126 for char in key):
        raise LedgerError(422, "idempotency key must contain visible ASCII only")


def _validate_reversal_details(reason_code: str, note: str | None) -> None:
    if reason_code not in REVERSAL_REASON_CODES:
        raise LedgerError(422, "invalid reversal reason code")
    if note is None:
        return
    if not isinstance(note, str) or not 1 <= len(note) <= 240 or note != note.strip():
        raise LedgerError(422, "reversal note must be 1 to 240 trimmed characters")


def _validate_request_id(request_id: str | None) -> None:
    if request_id is None:
        return
    if (
        not isinstance(request_id, str)
        or not 1 <= len(request_id) <= 64
        or request_id != request_id.strip()
        or any(not 33 <= ord(char) <= 126 for char in request_id)
    ):
        raise ValueError("request_id must be 1 to 64 visible ASCII characters")


def _validate_outbox_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("outbox payload must be an object")
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            # Match SQLAlchemy's default JSON encoder so the database's
            # payload::text bound cannot reject an object accepted here.
            ensure_ascii=True,
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ValueError("outbox payload must be JSON serializable") from exc
    if len(encoded) > MAX_OUTBOX_PAYLOAD_BYTES:
        raise ValueError("outbox payload exceeds 4096 bytes")
    return dict(payload)


def append_outbox_event(
    session: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    payload: dict[str, Any],
    request_id: str | None = None,
) -> OutboxEvent:
    """Stage a bounded event in the caller's current database transaction.

    This helper deliberately neither flushes nor commits. The ledger operation and
    its event therefore succeed or roll back together under the caller's boundary.
    """

    if event_type not in OUTBOX_EVENT_TYPES:
        raise ValueError("unsupported outbox event type")
    if aggregate_type not in OUTBOX_AGGREGATE_TYPES:
        raise ValueError("unsupported outbox aggregate type")
    if not isinstance(aggregate_id, UUID):
        raise TypeError("aggregate_id must be a UUID")
    _validate_request_id(request_id)
    safe_payload = _validate_outbox_payload(payload)

    # A database trigger acquires the transaction-order lock and assigns the
    # event ID at flush. Direct SQL therefore cannot bypass commit-order IDs.
    event = OutboxEvent(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        request_id=request_id,
        payload=safe_payload,
    )
    session.add(event)
    return event


def _balance(session: Session, account_id: UUID) -> Decimal:
    amount = session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount), Decimal("0.00"))).where(
            LedgerEntry.account_id == account_id
        )
    )
    return Decimal(amount or Decimal("0.00")).quantize(CENT)


def _lock_idempotency_key(session: Session, key: str) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": key},
    )


def _lock_accounts(session: Session, account_ids: set[UUID]) -> dict[UUID, Account]:
    accounts = session.scalars(
        select(Account)
        .where(Account.id.in_(account_ids))
        .order_by(Account.id)
        .with_for_update(key_share=True)
    ).all()
    return {account.id: account for account in accounts}


def _reversal_fingerprint(
    transaction_id: UUID, reason_code: str, note: str | None
) -> str:
    canonical = json.dumps(
        {
            "note": note,
            "operation": "reversal",
            "reason_code": reason_code,
            "transaction_id": str(transaction_id),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _after_first_reversal_entry(_posted_entry: LedgerEntry) -> None:
    """Test seam proving entries, transaction, and event share one rollback."""


def reverse_transaction(
    session: Session,
    transaction_id: UUID,
    idempotency_key: str,
    reason_code: str,
    *,
    note: str | None = None,
    request_id: str | None = None,
) -> IdempotencyResult:
    """Post one exact compensating transaction without mutating the original."""

    _validate_idempotency_key(idempotency_key)
    _validate_reversal_details(reason_code, note)
    _validate_request_id(request_id)
    request_fingerprint = _reversal_fingerprint(transaction_id, reason_code, note)

    with session.begin():
        # Same-key requests serialize before reading. A transaction-scoped
        # advisory lock is released automatically on either commit or rollback.
        _lock_idempotency_key(session, idempotency_key)
        if (
            session.get(
                Account,
                uuid5(ACCOUNT_CREATION_NAMESPACE, idempotency_key),
            )
            is not None
        ):
            raise LedgerError(
                409, "idempotency key was already used for another request"
            )
        replay = session.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.idempotency_key == idempotency_key
            )
        )
        if replay is not None:
            if (
                replay.type != "reversal"
                or replay.request_fingerprint != request_fingerprint
            ):
                raise LedgerError(
                    409, "idempotency key was already used for another request"
                )
            append_outbox_event(
                session,
                event_type="request.replayed",
                aggregate_type="ledger_transaction",
                aggregate_id=replay.id,
                request_id=request_id,
                payload={
                    "operation": "reversal",
                    "transaction_id": str(replay.id),
                },
            )
            session.flush()
            return IdempotencyResult(dict(replay.response_payload), replayed=True)

        original = session.scalar(
            select(LedgerTransaction)
            .where(LedgerTransaction.id == transaction_id)
            # The row is an immutable mutex for competing reversals. NO KEY
            # UPDATE still serializes those writers while remaining compatible
            # with reconciliation's foreign-key KEY SHARE checks.
            .with_for_update(key_share=True)
        )
        if original is None:
            raise LedgerError(404, "transaction not found")
        if original.type == "reversal":
            raise LedgerError(409, "reversals cannot be reversed")

        prior_reversal_id = session.scalar(
            select(LedgerTransaction.id).where(
                LedgerTransaction.reverses_transaction_id == original.id
            )
        )
        if prior_reversal_id is not None:
            raise LedgerError(409, "transaction was already reversed")

        # The account that received the original funds returns them. Account
        # locks are always acquired in UUID order, matching ordinary transfers.
        source_account_id = original.destination_account_id
        destination_account_id = original.source_account_id
        accounts = _lock_accounts(session, {source_account_id, destination_account_id})
        source = accounts.get(source_account_id)
        destination = accounts.get(destination_account_id)
        if source is None or destination is None:
            raise RuntimeError("ledger transaction references a missing account")
        if source.is_system:
            raise RuntimeError("reversal source must be a customer account")
        if (
            source.currency != original.currency
            or destination.currency != original.currency
        ):
            raise RuntimeError("ledger transaction account currency is inconsistent")

        if _balance(session, source.id) < original.amount:
            raise LedgerError(409, "insufficient funds to reverse transaction")

        reversal_id = uuid4()
        created_at = datetime.now(UTC)
        payload: dict[str, Any] = {
            "transaction_id": str(reversal_id),
            "reverses_transaction_id": str(original.id),
            "source_account_id": str(source.id),
            "destination_account_id": str(destination.id),
            "amount": _format_money(original.amount),
            "currency": original.currency,
            "reason_code": reason_code,
            "note": note,
            "created_at": created_at.isoformat(),
        }
        reversal = LedgerTransaction(
            id=reversal_id,
            type="reversal",
            amount=original.amount,
            currency=original.currency,
            source_account_id=source.id,
            destination_account_id=destination.id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            response_payload=payload,
            reverses_transaction_id=original.id,
            reversal_reason_code=reason_code,
            reversal_note=note,
            created_at=created_at,
        )
        source_entry = LedgerEntry(
            transaction_id=reversal.id,
            account_id=source.id,
            sequence=1,
            amount=-original.amount,
            currency=original.currency,
            created_at=created_at,
        )
        destination_entry = LedgerEntry(
            transaction_id=reversal.id,
            account_id=destination.id,
            sequence=2,
            amount=original.amount,
            currency=original.currency,
            created_at=created_at,
        )

        session.add(reversal)
        session.add(source_entry)
        session.flush()
        _after_first_reversal_entry(source_entry)
        session.add(destination_entry)
        append_outbox_event(
            session,
            event_type="reversal.created",
            aggregate_type="ledger_transaction",
            aggregate_id=reversal.id,
            request_id=request_id,
            payload={
                "amount": _format_money(reversal.amount),
                "currency": reversal.currency,
                "note": reversal.reversal_note,
                "reason_code": reversal.reversal_reason_code,
                "reverses_transaction_id": str(original.id),
                "transaction_id": str(reversal.id),
            },
        )
        session.flush()

    return IdempotencyResult(payload, replayed=False)


__all__ = [
    "ACCOUNT_CREATION_NAMESPACE",
    "OUTBOX_AGGREGATE_TYPES",
    "OUTBOX_EVENT_TYPES",
    "REVERSAL_REASON_CODES",
    "append_outbox_event",
    "reverse_transaction",
]
