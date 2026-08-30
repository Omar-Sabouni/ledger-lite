"""Provider reconciliation services.

Reconciliation classifies operational records around the immutable ledger; it
never repairs, rewrites, or annotates ledger transactions themselves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.orm import Session

from app.errors import LedgerError
from app.ledger import append_outbox_event
from app.models import (
    LedgerTransaction,
    OutboxEvent,
    ReconciliationItem,
    ReconciliationRun,
)
from app.pagination import CursorKind, decode_cursor, encode_cursor

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
LEDGER_ONLY_NAMESPACE = UUID("8deeb36d-fd29-4e6b-807a-5e299205a92e")

RESULTS = frozenset(
    {"pending", "matched", "provider_only", "ledger_only", "mismatched", "duplicate"}
)
RESOLUTION_STATUSES = frozenset({"open", "matched", "ignored"})
MISMATCH_CODES = frozenset(
    {
        "transaction_not_found",
        "amount_mismatch",
        "currency_mismatch",
        "transaction_type_mismatch",
        "outside_period",
        "duplicate_claim",
        "unclaimed_ledger_transaction",
    }
)


def _format_money(value: Decimal | int | None) -> str:
    return format(Decimal(value or ZERO).quantize(CENT), ".2f")


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise LedgerError(422, "limit must be between 1 and 100")


def _normalize_note(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise LedgerError(422, "a resolution reason is required")
        return None
    normalized = value.strip()
    if not normalized:
        if required:
            raise LedgerError(422, "a resolution reason is required")
        return None
    if len(normalized) > 240:
        raise LedgerError(422, "resolution note must not exceed 240 characters")
    return normalized


def _serialize_run(run: ReconciliationRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "provider": run.provider,
        "currency": run.currency,
        "period_start": _iso_utc(run.period_start),
        "period_end": _iso_utc(run.period_end),
        "status": run.status,
        "summary": dict(run.summary) if run.summary is not None else None,
        "created_at": _iso_utc(run.created_at),
        "completed_at": (
            _iso_utc(run.completed_at) if run.completed_at is not None else None
        ),
    }


def _serialize_item(item: ReconciliationItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "run_id": str(item.run_id),
        "provider_reference": item.provider_reference,
        "claimed_transaction_id": (
            str(item.claimed_transaction_id)
            if item.claimed_transaction_id is not None
            else None
        ),
        "matched_transaction_id": (
            str(item.matched_transaction_id)
            if item.matched_transaction_id is not None
            else None
        ),
        "amount": _format_money(item.amount),
        "currency": item.currency,
        "occurred_at": _iso_utc(item.occurred_at),
        "result": item.result,
        "mismatch_code": item.mismatch_code,
        "resolution_status": item.resolution_status,
        "resolution_note": item.resolution_note,
        "created_at": _iso_utc(item.created_at),
        "resolved_at": (
            _iso_utc(item.resolved_at) if item.resolved_at is not None else None
        ),
    }


def _add_event(
    session: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    request_id: str | None,
    payload: dict[str, Any],
    created_at: datetime,
) -> None:
    event = append_outbox_event(
        session,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        request_id=request_id,
        payload=payload,
    )
    event.created_at = created_at


def list_runs(session: Session) -> dict[str, Any]:
    runs = session.scalars(
        select(ReconciliationRun).order_by(
            ReconciliationRun.period_start.desc(), ReconciliationRun.id.desc()
        )
    ).all()
    return {"items": [_serialize_run(run) for run in runs]}


def get_run(session: Session, run_id: UUID) -> dict[str, Any]:
    run = session.get(ReconciliationRun, run_id)
    if run is None:
        raise LedgerError(404, "reconciliation run not found")
    return _serialize_run(run)


def execute_run(
    session: Session,
    run_id: UUID,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Classify a seeded provider statement exactly once."""

    with session.begin():
        # Every provider classification, ledger-only derivation and stored
        # volume total must observe the same committed ledger snapshot.
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        run = session.scalar(
            select(ReconciliationRun)
            .where(ReconciliationRun.id == run_id)
            .with_for_update()
        )
        if run is None:
            raise LedgerError(404, "reconciliation run not found")
        if run.status == "completed":
            return _serialize_run(run)
        if run.status != "pending":
            raise LedgerError(409, "reconciliation run cannot be executed")

        provider_items = session.scalars(
            select(ReconciliationItem)
            .where(
                ReconciliationItem.run_id == run.id,
                ReconciliationItem.result == "pending",
                ReconciliationItem.provider_reference.is_not(None),
            )
            .order_by(ReconciliationItem.occurred_at, ReconciliationItem.id)
            .with_for_update()
        ).all()

        claimed_ids = {
            item.claimed_transaction_id
            for item in provider_items
            if item.claimed_transaction_id is not None
        }
        claimed_transactions = {
            transaction.id: transaction
            for transaction in (
                session.scalars(
                    select(LedgerTransaction).where(
                        LedgerTransaction.id.in_(claimed_ids)
                    )
                ).all()
                if claimed_ids
                else []
            )
        }
        ledger_candidates = session.scalars(
            select(LedgerTransaction)
            .where(
                LedgerTransaction.currency == run.currency,
                LedgerTransaction.type.in_(("transfer", "reversal")),
                LedgerTransaction.created_at >= run.period_start,
                LedgerTransaction.created_at < run.period_end,
            )
            .order_by(LedgerTransaction.created_at, LedgerTransaction.id)
        ).all()

        seen_claims: set[UUID] = set()
        matched_ids: set[UUID] = set()
        now = datetime.now(UTC)
        for item in provider_items:
            claimed_id = item.claimed_transaction_id
            transaction = (
                claimed_transactions.get(claimed_id) if claimed_id is not None else None
            )

            if not (run.period_start <= item.occurred_at < run.period_end):
                item.result = "mismatched"
                item.mismatch_code = "outside_period"
            elif claimed_id is not None and claimed_id in seen_claims:
                item.result = "duplicate"
                item.mismatch_code = "duplicate_claim"
            elif transaction is None:
                item.result = "provider_only"
                item.mismatch_code = "transaction_not_found"
            elif transaction.type not in {"transfer", "reversal"}:
                item.result = "mismatched"
                item.mismatch_code = "transaction_type_mismatch"
            elif item.currency != run.currency or transaction.currency != run.currency:
                item.result = "mismatched"
                item.mismatch_code = "currency_mismatch"
            elif transaction.amount != item.amount:
                item.result = "mismatched"
                item.mismatch_code = "amount_mismatch"
            elif transaction.currency != item.currency:
                item.result = "mismatched"
                item.mismatch_code = "currency_mismatch"
            elif not (run.period_start <= transaction.created_at < run.period_end):
                item.result = "mismatched"
                item.mismatch_code = "outside_period"
            else:
                item.result = "matched"
                item.mismatch_code = None
                item.matched_transaction_id = transaction.id
                item.resolution_status = "matched"
                item.resolved_at = now
                matched_ids.add(transaction.id)

            if claimed_id is not None:
                seen_claims.add(claimed_id)

        for transaction in ledger_candidates:
            if transaction.id in matched_ids:
                continue
            ledger_only_id = uuid5(LEDGER_ONLY_NAMESPACE, f"{run.id}:{transaction.id}")
            # Runtime has no direct INSERT authority on reconciliation evidence.
            # This narrowly-scoped security-definer function derives every raw
            # field from the locked run and immutable transaction.
            session.execute(
                text(
                    "SELECT public.reconciliation_insert_ledger_only("
                    ":run_id, :transaction_id, :item_id)"
                ),
                {
                    "run_id": run.id,
                    "transaction_id": transaction.id,
                    "item_id": ledger_only_id,
                },
            )

        session.flush()
        run.status = "completed"
        run.completed_at = now
        run.summary = _calculate_summary(session, run)
        counts = run.summary["counts"]
        _add_event(
            session,
            event_type="reconciliation.completed",
            aggregate_type="reconciliation_run",
            aggregate_id=run.id,
            request_id=request_id,
            payload={
                "status": "completed",
                "matched": counts["matched"],
                "exceptions": counts["open_exceptions"],
            },
            created_at=now,
        )
        session.flush()
        return _serialize_run(run)


def list_items(
    session: Session,
    run_id: UUID,
    *,
    result: str | None = None,
    resolution_status: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> dict[str, Any]:
    _validate_limit(limit)
    if result is not None and result not in RESULTS:
        raise LedgerError(422, "reconciliation result is invalid")
    if resolution_status is not None and resolution_status not in RESOLUTION_STATUSES:
        raise LedgerError(422, "reconciliation resolution status is invalid")
    if session.get(ReconciliationRun, run_id) is None:
        raise LedgerError(404, "reconciliation run not found")

    filters: dict[str, object] = {
        "resolution_status": resolution_status,
        "result": result,
        "run_id": run_id,
    }
    conditions = [ReconciliationItem.run_id == run_id]
    if result is not None:
        conditions.append(ReconciliationItem.result == result)
    if resolution_status is not None:
        conditions.append(ReconciliationItem.resolution_status == resolution_status)

    position: tuple[datetime, UUID] | None = None
    high_water: tuple[datetime, UUID] | None = None
    if cursor is not None:
        decoded = decode_cursor(
            cursor,
            kind=CursorKind.RECONCILIATION_ITEMS,
            filters=filters,
        )
        position = decoded.position  # type: ignore[assignment]
        high_water = decoded.high_water or decoded.position  # type: ignore[assignment]
    else:
        first_key = session.execute(
            select(ReconciliationItem.created_at, ReconciliationItem.id)
            .where(*conditions)
            .order_by(
                ReconciliationItem.created_at.desc(),
                ReconciliationItem.id.desc(),
            )
            .limit(1)
        ).first()
        if first_key is not None:
            high_water = (first_key.created_at, first_key.id)

    query = select(ReconciliationItem).where(*conditions)
    if high_water is not None:
        query = query.where(
            tuple_(ReconciliationItem.created_at, ReconciliationItem.id)
            <= tuple_(*high_water)
        )
    if position is not None:
        query = query.where(
            tuple_(ReconciliationItem.created_at, ReconciliationItem.id)
            < tuple_(*position)
        )
    rows = session.scalars(
        query.order_by(
            ReconciliationItem.created_at.desc(), ReconciliationItem.id.desc()
        ).limit(limit + 1)
    ).all()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(
            kind=CursorKind.RECONCILIATION_ITEMS,
            filters=filters,
            position=(last.created_at, last.id),
            high_water=high_water,
        )
    return {
        "items": [_serialize_item(item) for item in page],
        "next_cursor": next_cursor,
    }


def match_item(
    session: Session,
    item_id: UUID,
    transaction_id: UUID,
    *,
    request_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Manually pair one provider row with a compatible ledger transaction."""

    normalized_note = _normalize_note(note, required=False)
    with session.begin():
        run_id = session.scalar(
            select(ReconciliationItem.run_id).where(ReconciliationItem.id == item_id)
        )
        if run_id is None:
            raise LedgerError(404, "reconciliation item not found")
        run = session.scalar(
            select(ReconciliationRun)
            .where(ReconciliationRun.id == run_id)
            .with_for_update()
        )
        item = session.scalar(
            select(ReconciliationItem)
            .where(ReconciliationItem.id == item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None or item is None:  # pragma: no cover - protected by FK/lock
            raise LedgerError(404, "reconciliation item not found")
        if run.status != "completed":
            raise LedgerError(409, "reconciliation run has not completed")
        if item.resolution_status != "open":
            manual_resolution_event = session.scalar(
                select(OutboxEvent.id)
                .where(
                    OutboxEvent.event_type == "reconciliation.resolved",
                    OutboxEvent.aggregate_type == "reconciliation_item",
                    OutboxEvent.aggregate_id == item.id,
                )
                .limit(1)
            )
            if (
                item.result == "matched"
                and item.resolution_status == "matched"
                and item.matched_transaction_id == transaction_id
                and item.resolution_note == normalized_note
                and manual_resolution_event is not None
            ):
                return _serialize_item(item)
            raise LedgerError(409, "reconciliation item is already resolved")
        if item.result == "ledger_only":
            raise LedgerError(409, "ledger-only items cannot be manually matched")

        transaction = session.get(LedgerTransaction, transaction_id)
        if transaction is None:
            raise LedgerError(404, "transaction not found")
        if transaction.type not in {"transfer", "reversal"}:
            raise LedgerError(409, "only transfers and reversals can be reconciled")
        if item.currency != run.currency or transaction.currency != run.currency:
            raise LedgerError(
                409, "transaction and provider item must use the run currency"
            )
        if transaction.amount != item.amount or transaction.currency != item.currency:
            raise LedgerError(
                409, "transaction amount and currency must match the provider item"
            )
        if not (
            run.period_start <= item.occurred_at < run.period_end
            and run.period_start <= transaction.created_at < run.period_end
        ):
            raise LedgerError(
                409, "provider item or transaction is outside the reconciliation period"
            )
        already_matched = session.scalar(
            select(ReconciliationItem.id).where(
                ReconciliationItem.run_id == run.id,
                ReconciliationItem.matched_transaction_id == transaction.id,
                ReconciliationItem.id != item.id,
            )
        )
        if already_matched is not None:
            raise LedgerError(409, "transaction is already matched in this run")

        # The matching provider row explains the corresponding ledger-only
        # exception. Lock it regardless of state so an earlier operator decision
        # cannot leave contradictory evidence for the same ledger transaction.
        ledger_only = session.scalar(
            select(ReconciliationItem)
            .where(
                ReconciliationItem.run_id == run.id,
                ReconciliationItem.result == "ledger_only",
                ReconciliationItem.claimed_transaction_id == transaction.id,
            )
            .with_for_update()
        )
        if ledger_only is not None and ledger_only.resolution_status != "open":
            raise LedgerError(
                409,
                "the corresponding ledger-only exception is already resolved",
            )

        now = datetime.now(UTC)
        item.matched_transaction_id = transaction.id
        item.result = "matched"
        item.mismatch_code = None
        item.resolution_status = "matched"
        item.resolution_note = normalized_note
        item.resolved_at = now

        if ledger_only is not None:
            ledger_only.resolution_status = "matched"
            ledger_only.resolution_note = "Resolved by manual provider match"
            ledger_only.resolved_at = now
            _add_event(
                session,
                event_type="reconciliation.resolved",
                aggregate_type="reconciliation_item",
                aggregate_id=ledger_only.id,
                request_id=request_id,
                payload={"resolution": "matched", "result": ledger_only.result},
                created_at=now,
            )

        session.flush()
        run.summary = _calculate_summary(session, run, preserve_gross_volume=True)
        _add_event(
            session,
            event_type="reconciliation.resolved",
            aggregate_type="reconciliation_item",
            aggregate_id=item.id,
            request_id=request_id,
            payload={"resolution": "matched", "result": item.result},
            created_at=now,
        )
        session.flush()
        return _serialize_item(item)


def ignore_item(
    session: Session,
    item_id: UUID,
    reason: str,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Resolve one exception without changing any ledger row."""

    normalized_reason = _normalize_note(reason, required=True)
    with session.begin():
        run_id = session.scalar(
            select(ReconciliationItem.run_id).where(ReconciliationItem.id == item_id)
        )
        if run_id is None:
            raise LedgerError(404, "reconciliation item not found")
        run = session.scalar(
            select(ReconciliationRun)
            .where(ReconciliationRun.id == run_id)
            .with_for_update()
        )
        item = session.scalar(
            select(ReconciliationItem)
            .where(ReconciliationItem.id == item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None or item is None:  # pragma: no cover - protected by FK/lock
            raise LedgerError(404, "reconciliation item not found")
        if run.status != "completed":
            raise LedgerError(409, "reconciliation run has not completed")
        if item.resolution_status != "open":
            if (
                item.resolution_status == "ignored"
                and item.resolution_note == normalized_reason
            ):
                return _serialize_item(item)
            raise LedgerError(409, "reconciliation item is already resolved")

        now = datetime.now(UTC)
        item.resolution_status = "ignored"
        item.resolution_note = normalized_reason
        item.resolved_at = now
        session.flush()
        run.summary = _calculate_summary(session, run, preserve_gross_volume=True)
        _add_event(
            session,
            event_type="reconciliation.resolved",
            aggregate_type="reconciliation_item",
            aggregate_id=item.id,
            request_id=request_id,
            payload={"resolution": "ignored", "result": item.result},
            created_at=now,
        )
        session.flush()
        return _serialize_item(item)


def _calculate_summary(
    session: Session,
    run: ReconciliationRun,
    *,
    preserve_gross_volume: bool = False,
) -> dict[str, Any]:
    items = session.scalars(
        select(ReconciliationItem).where(ReconciliationItem.run_id == run.id)
    ).all()
    counts = {result: 0 for result in RESULTS}
    open_exceptions = 0
    provider_total = ZERO
    for item in items:
        counts[item.result] += 1
        if item.resolution_status == "open" and item.result != "pending":
            open_exceptions += 1
        if item.provider_reference is not None and item.currency == run.currency:
            provider_total += item.amount

    if preserve_gross_volume:
        if run.summary is None or "gross_volume" not in run.summary:
            raise RuntimeError("completed reconciliation is missing volume evidence")
        gross_volume = dict(run.summary["gross_volume"])
    else:
        ledger_total = session.scalar(
            select(func.coalesce(func.sum(LedgerTransaction.amount), ZERO)).where(
                LedgerTransaction.currency == run.currency,
                LedgerTransaction.type.in_(("transfer", "reversal")),
                LedgerTransaction.created_at >= run.period_start,
                LedgerTransaction.created_at < run.period_end,
            )
        )
        ledger_total = Decimal(ledger_total or ZERO)
        gross_volume = {
            "currency": run.currency,
            "provider_total": _format_money(provider_total),
            "ledger_total": _format_money(ledger_total),
            "difference": _format_money(provider_total - ledger_total),
        }
    return {
        "counts": {
            "matched": counts["matched"],
            "provider_only": counts["provider_only"],
            "ledger_only": counts["ledger_only"],
            "mismatched": counts["mismatched"],
            "duplicate": counts["duplicate"],
            "open_exceptions": open_exceptions,
        },
        "gross_volume": gross_volume,
    }


__all__ = [
    "MISMATCH_CODES",
    "RESOLUTION_STATUSES",
    "RESULTS",
    "execute_run",
    "get_run",
    "ignore_item",
    "list_items",
    "list_runs",
    "match_item",
]
