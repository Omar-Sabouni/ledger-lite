"""Read models for the LedgerLite operator console.

The ledger tables remain the financial source of truth.  Every balance and
integrity signal in this module is calculated from immutable postings rather
than copied into a mutable projection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, text, tuple_
from sqlalchemy.orm import Session, aliased

from app.errors import LedgerError
from app.models import (
    Account,
    LedgerEntry,
    LedgerTransaction,
    OutboxEvent,
    ReconciliationItem,
)
from app.pagination import CursorKind, decode_cursor, encode_cursor

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def _format_money(value: Decimal | int | None) -> str:
    return format(Decimal(value or ZERO).quantize(CENT), ".2f")


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise LedgerError(422, "limit must be between 1 and 100")


def _validate_currency(currency: str | None) -> None:
    if currency is not None and (
        len(currency) != 3 or not currency.isascii() or not currency.isupper()
    ):
        raise LedgerError(422, "currency must be a three-letter uppercase code")


def _normalize_datetime(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise LedgerError(422, f"{field} must include a timezone")
    return value.astimezone(UTC)


def get_overview(session: Session) -> dict[str, Any]:
    """Return one compact, per-currency view of funds and invariants."""

    # PostgreSQL READ COMMITTED takes a new snapshot for each statement. The
    # overview intentionally combines several bounded aggregates, so pin them
    # to one read-only snapshot before the first SELECT.
    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    as_of = session.scalar(select(func.now())) or datetime.now(UTC)

    balances = (
        select(
            LedgerEntry.account_id.label("account_id"),
            func.sum(LedgerEntry.amount).label("balance"),
        )
        .group_by(LedgerEntry.account_id)
        .subquery()
    )
    balance_value = func.coalesce(balances.c.balance, ZERO)
    account_rows = session.execute(
        select(
            Account.currency,
            func.count(Account.id)
            .filter(Account.is_system.is_(False))
            .label("customer_accounts"),
            func.coalesce(
                func.sum(balance_value).filter(Account.is_system.is_(False)), ZERO
            ).label("customer_funds"),
            func.coalesce(
                func.sum(balance_value).filter(Account.is_system.is_(True)), ZERO
            ).label("clearing_position"),
            func.coalesce(func.sum(balance_value), ZERO).label("net_imbalance"),
        )
        .outerjoin(balances, balances.c.account_id == Account.id)
        .group_by(Account.currency)
    ).all()

    by_currency: dict[str, dict[str, Any]] = {}
    for row in account_rows:
        by_currency[row.currency] = {
            "currency": row.currency,
            "customer_accounts": int(row.customer_accounts),
            "total_customer_funds": _format_money(row.customer_funds),
            "clearing_balance": _format_money(row.clearing_position),
            "net_imbalance": _format_money(row.net_imbalance),
        }
    entry_stats = (
        select(
            LedgerTransaction.id.label("transaction_id"),
            func.count(LedgerEntry.id).label("entry_count"),
            func.coalesce(func.sum(LedgerEntry.amount), ZERO).label("posting_sum"),
            func.coalesce(
                func.bool_and(LedgerEntry.currency == LedgerTransaction.currency),
                False,
            ).label("currencies_match"),
        )
        .outerjoin(
            LedgerEntry,
            LedgerEntry.transaction_id == LedgerTransaction.id,
        )
        .group_by(LedgerTransaction.id)
        .subquery()
    )
    unbalanced_transactions = session.scalar(
        select(func.count())
        .select_from(entry_stats)
        .where(
            or_(
                entry_stats.c.entry_count != 2,
                entry_stats.c.posting_sum != ZERO,
                entry_stats.c.currencies_match.is_not(True),
            )
        )
    )

    total_transactions = session.scalar(select(func.count(LedgerTransaction.id)))
    total_entries = session.scalar(select(func.count(LedgerEntry.id)))
    reversal_count = session.scalar(
        select(func.count(LedgerTransaction.id)).where(
            LedgerTransaction.type == "reversal"
        )
    )
    replay_count = session.scalar(
        select(func.count(OutboxEvent.id)).where(
            OutboxEvent.event_type == "request.replayed"
        )
    )
    open_exceptions = session.scalar(
        select(func.count(ReconciliationItem.id)).where(
            ReconciliationItem.resolution_status == "open",
            ReconciliationItem.result != "pending",
        )
    )

    return {
        "as_of": as_of.isoformat(),
        "currencies": [by_currency[key] for key in sorted(by_currency)],
        "integrity": {
            "transaction_count": int(total_transactions or 0),
            "entry_count": int(total_entries or 0),
            "reversal_count": int(reversal_count or 0),
            "unbalanced_transaction_count": int(unbalanced_transactions or 0),
            "replay_count": int(replay_count or 0),
            "open_reconciliation_exceptions": int(open_exceptions or 0),
        },
    }


def list_accounts(
    session: Session,
    *,
    currency: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List customer accounts using a filter-bound descending keyset."""

    _validate_limit(limit)
    _validate_currency(currency)
    filters: dict[str, object] = {"currency": currency}

    conditions = [Account.is_system.is_(False)]
    if currency is not None:
        conditions.append(Account.currency == currency)

    position: tuple[datetime, UUID] | None = None
    high_water: tuple[datetime, UUID] | None = None
    if cursor is not None:
        decoded = decode_cursor(cursor, kind=CursorKind.ACCOUNTS, filters=filters)
        position = decoded.position  # type: ignore[assignment]
        high_water = decoded.high_water or decoded.position  # type: ignore[assignment]
    else:
        first_key = session.execute(
            select(Account.created_at, Account.id)
            .where(*conditions)
            .order_by(Account.created_at.desc(), Account.id.desc())
            .limit(1)
        ).first()
        if first_key is not None:
            high_water = (first_key.created_at, first_key.id)

    balances = (
        select(
            LedgerEntry.account_id.label("account_id"),
            func.sum(LedgerEntry.amount).label("balance"),
        )
        .group_by(LedgerEntry.account_id)
        .subquery()
    )
    query = (
        select(
            Account,
            func.coalesce(balances.c.balance, ZERO).label("balance"),
        )
        .outerjoin(balances, balances.c.account_id == Account.id)
        .where(*conditions)
    )
    if high_water is not None:
        query = query.where(
            tuple_(Account.created_at, Account.id) <= tuple_(*high_water)
        )
    if position is not None:
        query = query.where(tuple_(Account.created_at, Account.id) < tuple_(*position))
    rows = session.execute(
        query.order_by(Account.created_at.desc(), Account.id.desc()).limit(limit + 1)
    ).all()

    has_more = len(rows) > limit
    page = rows[:limit]
    items = [
        {
            "id": str(row.Account.id),
            "display_name": row.Account.display_name,
            "currency": row.Account.currency,
            "balance": _format_money(row.balance),
            "created_at": row.Account.created_at.isoformat(),
        }
        for row in page
    ]
    next_cursor = None
    if has_more and page:
        last = page[-1].Account
        next_cursor = encode_cursor(
            kind=CursorKind.ACCOUNTS,
            filters=filters,
            position=(last.created_at, last.id),
            high_water=high_water,
        )
    return {"items": items, "next_cursor": next_cursor}


def get_account_statement(
    session: Session,
    account_id: UUID,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return a descending statement page with exact running balances."""

    _validate_limit(limit)
    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    account = session.get(Account, account_id)
    if account is None or account.is_system:
        raise LedgerError(404, "account not found")

    filters: dict[str, object] = {"account_id": account_id}
    position: tuple[int, UUID, int] | None = None
    high_water: tuple[int, UUID, int] | None = None
    if cursor is not None:
        decoded = decode_cursor(cursor, kind=CursorKind.STATEMENT, filters=filters)
        position = decoded.position  # type: ignore[assignment]
        high_water = decoded.high_water or decoded.position  # type: ignore[assignment]
    else:
        first_key = session.execute(
            select(
                LedgerTransaction.posting_sequence,
                LedgerEntry.transaction_id,
                LedgerEntry.sequence,
            )
            .join(
                LedgerTransaction,
                LedgerTransaction.id == LedgerEntry.transaction_id,
            )
            .where(LedgerEntry.account_id == account.id)
            .order_by(
                LedgerTransaction.posting_sequence.desc(),
                LedgerEntry.transaction_id.desc(),
                LedgerEntry.sequence.desc(),
            )
            .limit(1)
        ).first()
        if first_key is not None:
            high_water = (
                first_key.posting_sequence,
                first_key.transaction_id,
                first_key.sequence,
            )

    statement_rows = (
        select(
            LedgerEntry.id.label("entry_id"),
            LedgerEntry.transaction_id,
            LedgerEntry.sequence,
            LedgerEntry.amount,
            LedgerEntry.currency,
            LedgerEntry.created_at,
            LedgerTransaction.posting_sequence,
            LedgerTransaction.type.label("transaction_type"),
            LedgerTransaction.source_account_id,
            LedgerTransaction.destination_account_id,
            func.sum(LedgerEntry.amount)
            .over(
                order_by=(
                    LedgerTransaction.posting_sequence.asc(),
                    LedgerEntry.transaction_id.asc(),
                    LedgerEntry.sequence.asc(),
                ),
                rows=(None, 0),
            )
            .label("balance_after"),
        )
        .join(
            LedgerTransaction,
            LedgerTransaction.id == LedgerEntry.transaction_id,
        )
        .where(LedgerEntry.account_id == account.id)
        .subquery()
    )
    query = select(statement_rows)
    key_columns = (
        statement_rows.c.posting_sequence,
        statement_rows.c.transaction_id,
        statement_rows.c.sequence,
    )
    if high_water is not None:
        query = query.where(tuple_(*key_columns) <= tuple_(*high_water))
    if position is not None:
        query = query.where(tuple_(*key_columns) < tuple_(*position))
    rows = session.execute(
        query.order_by(*(column.desc() for column in key_columns)).limit(limit + 1)
    ).all()

    balance_conditions = [LedgerEntry.account_id == account.id]
    if high_water is not None:
        balance_conditions.append(
            tuple_(
                LedgerTransaction.posting_sequence,
                LedgerEntry.transaction_id,
                LedgerEntry.sequence,
            )
            <= tuple_(*high_water)
        )
    current_balance = session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount), ZERO))
        .join(
            LedgerTransaction,
            LedgerTransaction.id == LedgerEntry.transaction_id,
        )
        .where(*balance_conditions)
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    items: list[dict[str, Any]] = []
    for row in page:
        counterparty: UUID | None = None
        if row.transaction_type in {"transfer", "reversal"}:
            counterparty = (
                row.destination_account_id
                if row.source_account_id == account.id
                else row.source_account_id
            )
        items.append(
            {
                "id": str(row.entry_id),
                "transaction_id": str(row.transaction_id),
                "type": row.transaction_type,
                "amount": _format_money(row.amount),
                "currency": row.currency,
                "created_at": row.created_at.isoformat(),
                "counterparty_account_id": (
                    str(counterparty) if counterparty is not None else None
                ),
                "balance_after": _format_money(row.balance_after),
            }
        )

    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(
            kind=CursorKind.STATEMENT,
            filters=filters,
            position=(
                last.posting_sequence,
                last.transaction_id,
                last.sequence,
            ),
            high_water=high_water,
        )
    return {
        "account": {
            "id": str(account.id),
            "display_name": account.display_name,
            "currency": account.currency,
            "balance": _format_money(current_balance),
            "created_at": account.created_at.isoformat(),
        },
        "balance": _format_money(current_balance),
        "items": items,
        "next_cursor": next_cursor,
    }


def list_transactions(
    session: Session,
    *,
    currency: str | None = None,
    transaction_type: str | None = None,
    account_id: UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List transactions with exact filters and descending keyset paging."""

    _validate_limit(limit)
    _validate_currency(currency)
    if transaction_type is not None and transaction_type not in {
        "deposit",
        "transfer",
        "reversal",
    }:
        raise LedgerError(422, "transaction type is invalid")
    date_from = _normalize_datetime(date_from, "date_from")
    date_to = _normalize_datetime(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from >= date_to:
        raise LedgerError(422, "date_from must be before date_to")

    filters: dict[str, object] = {
        "account_id": account_id,
        "currency": currency,
        "date_from": date_from,
        "date_to": date_to,
        "type": transaction_type,
    }
    conditions = []
    if currency is not None:
        conditions.append(LedgerTransaction.currency == currency)
    if transaction_type is not None:
        conditions.append(LedgerTransaction.type == transaction_type)
    if account_id is not None:
        conditions.append(
            or_(
                LedgerTransaction.source_account_id == account_id,
                LedgerTransaction.destination_account_id == account_id,
            )
        )
    if date_from is not None:
        conditions.append(LedgerTransaction.created_at >= date_from)
    if date_to is not None:
        conditions.append(LedgerTransaction.created_at < date_to)

    position: tuple[int, UUID] | None = None
    high_water: tuple[int, UUID] | None = None
    if cursor is not None:
        decoded = decode_cursor(cursor, kind=CursorKind.TRANSACTIONS, filters=filters)
        position = decoded.position  # type: ignore[assignment]
        high_water = decoded.high_water or decoded.position  # type: ignore[assignment]
    else:
        first_key = session.execute(
            select(LedgerTransaction.posting_sequence, LedgerTransaction.id)
            .where(*conditions)
            .order_by(
                LedgerTransaction.posting_sequence.desc(),
                LedgerTransaction.id.desc(),
            )
            .limit(1)
        ).first()
        if first_key is not None:
            high_water = (first_key.posting_sequence, first_key.id)

    source_account = aliased(Account)
    destination_account = aliased(Account)
    reversal = aliased(LedgerTransaction)
    reversed_by_id = (
        select(reversal.id)
        .where(reversal.reverses_transaction_id == LedgerTransaction.id)
        .correlate(LedgerTransaction)
        .scalar_subquery()
    )
    query = (
        select(
            LedgerTransaction,
            source_account.display_name.label("source_display_name"),
            destination_account.display_name.label("destination_display_name"),
            reversed_by_id.label("reversed_by_transaction_id"),
        )
        .join(source_account, source_account.id == LedgerTransaction.source_account_id)
        .join(
            destination_account,
            destination_account.id == LedgerTransaction.destination_account_id,
        )
        .where(*conditions)
    )
    if high_water is not None:
        query = query.where(
            tuple_(LedgerTransaction.posting_sequence, LedgerTransaction.id)
            <= tuple_(*high_water)
        )
    if position is not None:
        query = query.where(
            tuple_(LedgerTransaction.posting_sequence, LedgerTransaction.id)
            < tuple_(*position)
        )
    rows = session.execute(
        query.order_by(
            LedgerTransaction.posting_sequence.desc(), LedgerTransaction.id.desc()
        ).limit(limit + 1)
    ).all()

    has_more = len(rows) > limit
    page = rows[:limit]
    items = [
        {
            "id": str(row.LedgerTransaction.id),
            "type": row.LedgerTransaction.type,
            "amount": _format_money(row.LedgerTransaction.amount),
            "currency": row.LedgerTransaction.currency,
            "source_account_id": str(row.LedgerTransaction.source_account_id),
            "source_display_name": row.source_display_name,
            "destination_account_id": str(row.LedgerTransaction.destination_account_id),
            "destination_display_name": row.destination_display_name,
            "reverses_transaction_id": (
                str(row.LedgerTransaction.reverses_transaction_id)
                if row.LedgerTransaction.reverses_transaction_id is not None
                else None
            ),
            "reversed_by_transaction_id": (
                str(row.reversed_by_transaction_id)
                if row.reversed_by_transaction_id is not None
                else None
            ),
            "reversal_reason_code": row.LedgerTransaction.reversal_reason_code,
            "reversal_note": row.LedgerTransaction.reversal_note,
            "created_at": row.LedgerTransaction.created_at.isoformat(),
        }
        for row in page
    ]
    next_cursor = None
    if has_more and page:
        last = page[-1].LedgerTransaction
        next_cursor = encode_cursor(
            kind=CursorKind.TRANSACTIONS,
            filters=filters,
            position=(last.posting_sequence, last.id),
            high_water=high_water,
        )
    return {"items": items, "next_cursor": next_cursor}


def get_transaction(session: Session, transaction_id: UUID) -> dict[str, Any]:
    """Return one transaction without exposing idempotency internals."""

    source_account = aliased(Account)
    destination_account = aliased(Account)
    row = session.execute(
        select(
            LedgerTransaction,
            source_account.display_name.label("source_display_name"),
            source_account.is_system.label("source_is_system"),
            destination_account.display_name.label("destination_display_name"),
            destination_account.is_system.label("destination_is_system"),
        )
        .join(source_account, source_account.id == LedgerTransaction.source_account_id)
        .join(
            destination_account,
            destination_account.id == LedgerTransaction.destination_account_id,
        )
        .where(LedgerTransaction.id == transaction_id)
    ).one_or_none()
    if row is None:
        raise LedgerError(404, "transaction not found")

    transaction = row.LedgerTransaction
    posting_rows = session.execute(
        select(LedgerEntry, Account.display_name)
        .join(Account, Account.id == LedgerEntry.account_id)
        .where(LedgerEntry.transaction_id == transaction.id)
        .order_by(LedgerEntry.sequence)
    ).all()
    reversed_by = session.scalar(
        select(LedgerTransaction.id).where(
            LedgerTransaction.reverses_transaction_id == transaction.id
        )
    )

    posting_sum = sum((posting.LedgerEntry.amount for posting in posting_rows), ZERO)
    currency_consistent = all(
        posting.LedgerEntry.currency == transaction.currency for posting in posting_rows
    )
    entries = [
        {
            "id": str(posting.LedgerEntry.id),
            "sequence": posting.LedgerEntry.sequence,
            "account_id": str(posting.LedgerEntry.account_id),
            "account_display_name": posting.display_name,
            "amount": _format_money(posting.LedgerEntry.amount),
            "currency": posting.LedgerEntry.currency,
            "created_at": posting.LedgerEntry.created_at.isoformat(),
        }
        for posting in posting_rows
    ]
    return {
        "id": str(transaction.id),
        "type": transaction.type,
        "amount": _format_money(transaction.amount),
        "currency": transaction.currency,
        "source_account_id": str(transaction.source_account_id),
        "source_display_name": row.source_display_name,
        "destination_account_id": str(transaction.destination_account_id),
        "destination_display_name": row.destination_display_name,
        "reverses_transaction_id": (
            str(transaction.reverses_transaction_id)
            if transaction.reverses_transaction_id is not None
            else None
        ),
        "reversed_by_transaction_id": (
            str(reversed_by) if reversed_by is not None else None
        ),
        "reversal_reason_code": transaction.reversal_reason_code,
        "reversal_note": transaction.reversal_note,
        "created_at": transaction.created_at.isoformat(),
        "entries": entries,
        "integrity": {
            "entry_count": len(entries),
            "posting_sum": _format_money(posting_sum),
            "balanced": len(entries) == 2 and posting_sum == ZERO,
            "currency_consistent": currency_consistent,
        },
    }


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "get_account_statement",
    "get_overview",
    "get_transaction",
    "list_accounts",
    "list_transactions",
]
