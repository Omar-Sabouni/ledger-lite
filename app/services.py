from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from app.errors import LedgerError
from app.ledger import ACCOUNT_CREATION_NAMESPACE, append_outbox_event
from app.models import Account, IdempotencyResult, LedgerEntry, LedgerTransaction

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("999999999999999999.99")
CLEARING_NAMESPACE = UUID("5edf3a58-bfbd-4a31-b2fc-9f4b4b6ab4dd")


def _format_money(amount: Decimal) -> str:
    return format(amount.quantize(CENT), ".2f")


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _normalize_amount(amount: Decimal) -> Decimal:
    """Defend the domain boundary even when a caller bypasses API validation."""

    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise LedgerError(422, "amount must be a finite decimal")
    if amount <= 0:
        raise LedgerError(422, "amount must be greater than zero")
    if amount > MAX_AMOUNT:
        raise LedgerError(422, "amount exceeds NUMERIC(20, 2)")
    try:
        normalized = amount.quantize(CENT)
    except InvalidOperation as exc:
        raise LedgerError(422, "amount is not a valid decimal") from exc
    if amount != normalized:
        raise LedgerError(422, "amount must have at most 2 decimals")
    return normalized


def _validate_idempotency_key(key: str) -> None:
    if not 1 <= len(key) <= 255 or any(not 33 <= ord(char) <= 126 for char in key):
        raise LedgerError(422, "idempotency key must contain visible ASCII only")


def _normalize_display_name(display_name: str | None) -> str | None:
    if display_name is None:
        return None
    if not isinstance(display_name, str):
        raise LedgerError(422, "display name must be text")
    normalized = display_name.strip()
    if not 1 <= len(normalized) <= 80:
        raise LedgerError(422, "display name must be 1 to 80 characters")
    return normalized


def _after_first_entry(_posted_entry: LedgerEntry) -> None:
    """Test seam used to prove a mid-posting exception rolls everything back."""


def _balance(session: Session, account_id: UUID) -> Decimal:
    amount = session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount), Decimal("0.00"))).where(
            LedgerEntry.account_id == account_id
        )
    )
    return Decimal(amount or Decimal("0.00")).quantize(CENT)


def _clearing_account_id(currency: str) -> UUID:
    return uuid5(CLEARING_NAMESPACE, currency)


def _ensure_clearing_account(session: Session, currency: str) -> Account:
    account_id = _clearing_account_id(currency)
    system_key = f"clearing:{currency}"
    created_at = datetime.now(UTC)

    session.execute(
        postgresql_insert(Account)
        .values(
            id=account_id,
            currency=currency,
            is_system=True,
            system_key=system_key,
            created_at=created_at,
        )
        .on_conflict_do_nothing()
    )

    account = session.get(Account, account_id)
    if (
        account is None
        or account.currency != currency
        or not account.is_system
        or account.system_key != system_key
    ):
        raise RuntimeError("currency clearing account is inconsistent")
    return account


def _lock_accounts(session: Session, account_ids: set[UUID]) -> dict[UUID, Account]:
    accounts = session.scalars(
        select(Account)
        .where(Account.id.in_(account_ids))
        .order_by(Account.id)
        .with_for_update(key_share=True)
    ).all()
    return {account.id: account for account in accounts}


def _transfer_fingerprint(
    source_account_id: UUID,
    destination_account_id: UUID,
    amount: Decimal,
) -> str:
    canonical = f"{source_account_id}:{destination_account_id}:{_format_money(amount)}"
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _deposit_fingerprint(account_id: UUID, amount: Decimal) -> str:
    canonical = f"deposit:{account_id}:{_format_money(amount)}"
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _lock_idempotency_key(session: Session, key: str) -> None:
    # The unique constraint is the final backstop. This transaction-scoped lock
    # makes a same-key race deterministic before either request reads or posts.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": key},
    )


def create_account(
    session: Session,
    currency: str,
    *,
    display_name: str | None = None,
    idempotency_key: str | None = None,
) -> IdempotencyResult:
    display_name = _normalize_display_name(display_name)
    if idempotency_key is not None:
        _validate_idempotency_key(idempotency_key)
    account_id = (
        uuid5(ACCOUNT_CREATION_NAMESPACE, idempotency_key)
        if idempotency_key is not None
        else uuid4()
    )
    with session.begin():
        if idempotency_key is not None:
            _lock_idempotency_key(session, idempotency_key)
            movement = session.scalar(
                select(LedgerTransaction.id).where(
                    LedgerTransaction.idempotency_key == idempotency_key
                )
            )
            if movement is not None:
                raise LedgerError(
                    409, "idempotency key was already used for another request"
                )
            existing = session.get(Account, account_id)
            if existing is not None:
                if (
                    existing.is_system
                    or existing.currency != currency
                    or existing.display_name != display_name
                ):
                    raise LedgerError(
                        409,
                        "idempotency key was already used for another request",
                    )
                return IdempotencyResult(
                    {
                        "id": str(existing.id),
                        "display_name": existing.display_name,
                        "currency": existing.currency,
                        "balance": "0.00",
                        "created_at": _iso_utc(existing.created_at),
                    },
                    replayed=True,
                )
        account = Account(
            id=account_id,
            currency=currency,
            is_system=False,
            display_name=display_name,
        )
        session.add(account)
        session.flush()
        return IdempotencyResult(
            {
                "id": str(account.id),
                "display_name": account.display_name,
                "currency": account.currency,
                "balance": "0.00",
                "created_at": _iso_utc(account.created_at),
            },
            replayed=False,
        )


def deposit(
    session: Session,
    account_id: UUID,
    amount: Decimal,
    *,
    idempotency_key: str | None = None,
    transaction_id: UUID | None = None,
    entry_ids: tuple[UUID, UUID] | None = None,
    created_at: datetime | None = None,
    request_id: str | None = None,
) -> IdempotencyResult:
    amount = _normalize_amount(amount)
    if idempotency_key is not None:
        _validate_idempotency_key(idempotency_key)

    transaction_id = transaction_id or uuid4()
    entry_ids = entry_ids or (uuid4(), uuid4())
    with session.begin():
        return _deposit_in_transaction(
            session,
            account_id,
            amount,
            idempotency_key=idempotency_key,
            transaction_id=transaction_id,
            entry_ids=entry_ids,
            created_at=created_at,
            request_id=request_id,
        )


def _deposit_in_transaction(
    session: Session,
    account_id: UUID,
    amount: Decimal,
    *,
    idempotency_key: str | None,
    transaction_id: UUID,
    entry_ids: tuple[UUID, UUID],
    created_at: datetime | None,
    request_id: str | None = None,
) -> IdempotencyResult:
    request_fingerprint = (
        _deposit_fingerprint(account_id, amount)
        if idempotency_key is not None
        else None
    )
    if idempotency_key is not None:
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
        existing = session.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            if (
                existing.type != "deposit"
                or existing.request_fingerprint != request_fingerprint
            ):
                raise LedgerError(
                    409, "idempotency key was already used for another request"
                )
            append_outbox_event(
                session,
                event_type="request.replayed",
                aggregate_type="ledger_transaction",
                aggregate_id=existing.id,
                request_id=request_id,
                payload={
                    "operation": "deposit",
                    "transaction_id": str(existing.id),
                },
            )
            session.flush()
            return IdempotencyResult(dict(existing.response_payload), replayed=True)

    already_posted = session.get(LedgerTransaction, transaction_id)
    if already_posted is not None:
        expected_clearing_id = _clearing_account_id(already_posted.currency)
        posted_entries = session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.transaction_id == transaction_id)
            .order_by(LedgerEntry.sequence)
        ).all()
        if created_at is None:
            raise RuntimeError("existing seeded transaction requires a timestamp")
        expected_entries = [
            (
                entry_ids[0],
                1,
                account_id,
                amount,
                already_posted.currency,
                created_at,
            ),
            (
                entry_ids[1],
                2,
                expected_clearing_id,
                -amount,
                already_posted.currency,
                created_at,
            ),
        ]
        actual_entries = [
            (
                entry.id,
                entry.sequence,
                entry.account_id,
                entry.amount,
                entry.currency,
                entry.created_at,
            )
            for entry in posted_entries
        ]
        if (
            already_posted.type != "deposit"
            or already_posted.source_account_id != expected_clearing_id
            or already_posted.destination_account_id != account_id
            or already_posted.amount != amount
            or already_posted.created_at != created_at
            or already_posted.idempotency_key != idempotency_key
            or already_posted.request_fingerprint != request_fingerprint
            or actual_entries != expected_entries
        ):
            raise RuntimeError("seed transaction identifier is inconsistent")
        stored_payload = dict(already_posted.response_payload)
        if (
            stored_payload.get("transaction_id") != str(transaction_id)
            or stored_payload.get("account_id") != str(account_id)
            or stored_payload.get("amount") != _format_money(amount)
            or stored_payload.get("currency") != already_posted.currency
            or stored_payload.get("created_at") != created_at.isoformat()
        ):
            raise RuntimeError("seed transaction response is inconsistent")
        return IdempotencyResult(stored_payload, replayed=True)

    candidate = session.get(Account, account_id)
    if candidate is None or candidate.is_system:
        raise LedgerError(404, "account not found")

    clearing = _ensure_clearing_account(session, candidate.currency)
    locked = _lock_accounts(session, {candidate.id})
    account = locked.get(candidate.id)
    if account is None or account.is_system:
        raise LedgerError(404, "account not found")

    # A normal request receives its journal timestamp only after it owns the
    # account lock. This keeps statement time order aligned with serialized
    # posting order under contention. Seed callers may supply a fixed time.
    created_at = created_at or datetime.now(UTC)
    balance_after = _balance(session, account.id) + amount
    payload = {
        "transaction_id": str(transaction_id),
        "account_id": str(account.id),
        "amount": _format_money(amount),
        "currency": account.currency,
        "balance": _format_money(balance_after),
        "created_at": created_at.isoformat(),
    }
    transaction = LedgerTransaction(
        id=transaction_id,
        type="deposit",
        amount=amount,
        currency=account.currency,
        source_account_id=clearing.id,
        destination_account_id=account.id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        response_payload=payload,
        created_at=created_at,
    )
    customer_entry = LedgerEntry(
        id=entry_ids[0],
        transaction_id=transaction_id,
        account_id=account.id,
        sequence=1,
        amount=amount,
        currency=account.currency,
        created_at=created_at,
    )
    clearing_entry = LedgerEntry(
        id=entry_ids[1],
        transaction_id=transaction_id,
        account_id=clearing.id,
        sequence=2,
        amount=-amount,
        currency=account.currency,
        created_at=created_at,
    )

    session.add(transaction)
    session.add(customer_entry)
    session.flush()
    _after_first_entry(customer_entry)
    session.add(clearing_entry)
    append_outbox_event(
        session,
        event_type="posting.created",
        aggregate_type="ledger_transaction",
        aggregate_id=transaction.id,
        request_id=request_id,
        payload={
            "account_id": str(account.id),
            "amount": _format_money(amount),
            "currency": account.currency,
            "operation": "deposit",
            "transaction_id": str(transaction.id),
        },
    )
    session.flush()

    return IdempotencyResult(payload, replayed=False)


def transfer(
    session: Session,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount: Decimal,
    idempotency_key: str,
    *,
    request_id: str | None = None,
) -> IdempotencyResult:
    amount = _normalize_amount(amount)
    _validate_idempotency_key(idempotency_key)
    if source_account_id == destination_account_id:
        raise LedgerError(422, "source and destination accounts must differ")

    request_fingerprint = _transfer_fingerprint(
        source_account_id, destination_account_id, amount
    )
    payload: dict[str, Any]

    with session.begin():
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
        existing = session.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            if (
                existing.type != "transfer"
                or existing.request_fingerprint != request_fingerprint
            ):
                raise LedgerError(
                    409, "idempotency key was already used for another request"
                )
            append_outbox_event(
                session,
                event_type="request.replayed",
                aggregate_type="ledger_transaction",
                aggregate_id=existing.id,
                request_id=request_id,
                payload={
                    "operation": "transfer",
                    "transaction_id": str(existing.id),
                },
            )
            session.flush()
            return IdempotencyResult(dict(existing.response_payload), replayed=True)

        locked = _lock_accounts(session, {source_account_id, destination_account_id})
        source = locked.get(source_account_id)
        destination = locked.get(destination_account_id)
        if (
            source is None
            or destination is None
            or source.is_system
            or destination.is_system
        ):
            raise LedgerError(404, "account not found")
        if source.currency != destination.currency:
            raise LedgerError(422, "accounts must have the same currency")

        source_balance = _balance(session, source.id)
        if source_balance < amount:
            raise LedgerError(409, "insufficient funds")

        transaction_id = uuid4()
        created_at = datetime.now(UTC)
        payload = {
            "transaction_id": str(transaction_id),
            "source_account_id": str(source.id),
            "destination_account_id": str(destination.id),
            "amount": _format_money(amount),
            "currency": source.currency,
            "created_at": created_at.isoformat(),
        }
        transaction = LedgerTransaction(
            id=transaction_id,
            type="transfer",
            amount=amount,
            currency=source.currency,
            source_account_id=source.id,
            destination_account_id=destination.id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            response_payload=payload,
            created_at=created_at,
        )
        source_entry = LedgerEntry(
            transaction_id=transaction_id,
            account_id=source.id,
            sequence=1,
            amount=-amount,
            currency=source.currency,
            created_at=created_at,
        )
        destination_entry = LedgerEntry(
            transaction_id=transaction_id,
            account_id=destination.id,
            sequence=2,
            amount=amount,
            currency=source.currency,
            created_at=created_at,
        )

        session.add(transaction)
        session.add(source_entry)
        session.flush()
        _after_first_entry(source_entry)
        session.add(destination_entry)
        append_outbox_event(
            session,
            event_type="posting.created",
            aggregate_type="ledger_transaction",
            aggregate_id=transaction.id,
            request_id=request_id,
            payload={
                "amount": _format_money(amount),
                "currency": source.currency,
                "destination_account_id": str(destination.id),
                "operation": "transfer",
                "source_account_id": str(source.id),
                "transaction_id": str(transaction.id),
            },
        )
        session.flush()

    return IdempotencyResult(payload, replayed=False)


def get_statement(session: Session, account_id: UUID) -> dict[str, Any]:
    account = session.get(Account, account_id)
    if account is None or account.is_system:
        raise LedgerError(404, "account not found")

    rows = session.execute(
        select(LedgerEntry, LedgerTransaction)
        .join(
            LedgerTransaction,
            LedgerTransaction.id == LedgerEntry.transaction_id,
        )
        .where(LedgerEntry.account_id == account.id)
        .order_by(
            LedgerEntry.created_at.desc(),
            LedgerEntry.transaction_id.desc(),
            LedgerEntry.sequence.desc(),
        )
    ).all()

    entries: list[dict[str, Any]] = []
    statement_balance = Decimal("0.00")
    for entry, transaction in rows:
        statement_balance += entry.amount
        counterparty: UUID | None = None
        if transaction.type in {"transfer", "reversal"}:
            counterparty = (
                transaction.destination_account_id
                if transaction.source_account_id == account.id
                else transaction.source_account_id
            )
        entries.append(
            {
                "transaction_id": str(transaction.id),
                "type": transaction.type,
                "amount": _format_money(entry.amount),
                "currency": entry.currency,
                "created_at": transaction.created_at.isoformat(),
                "counterparty_account_id": (
                    str(counterparty) if counterparty is not None else None
                ),
            }
        )

    return {
        "account_id": str(account.id),
        "currency": account.currency,
        # Derive the balance from the exact rows returned above. PostgreSQL's
        # READ COMMITTED isolation gives each statement its own snapshot; a
        # second SUM query could otherwise include a posting absent here.
        "balance": _format_money(statement_balance),
        "entries": entries,
    }
