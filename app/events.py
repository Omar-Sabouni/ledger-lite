"""Committed transactional-outbox delivery through resumable server-sent events."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import StreamingResponse

from app.database import SessionLocal
from app.errors import LedgerError
from app.models import OutboxEvent
from app.observability import METRICS, request_id_from_scope

_LAST_EVENT_ID_PATTERN: Final = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_MAX_EVENT_ID: Final = 9_223_372_036_854_775_807
_EVENT_TYPES: Final = frozenset(
    {
        "posting.created",
        "reconciliation.completed",
        "reconciliation.resolved",
        "request.replayed",
        "reversal.created",
    }
)
_AGGREGATE_TYPES: Final = frozenset(
    {"ledger_transaction", "reconciliation_item", "reconciliation_run"}
)
_LOGGER = logging.getLogger("ledgerlite.events")
SessionFactory = Callable[[], Session]


class InvalidEventCursor(LedgerError):
    def __init__(self) -> None:
        super().__init__(
            422,
            "Last-Event-ID is malformed",
            code="invalid_event_cursor",
        )


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    request_id: str | None
    created_at: str
    payload: dict[str, Any]


def parse_last_event_id(value: str | None) -> int | None:
    """Parse a bounded PostgreSQL bigint cursor without accepting whitespace."""

    if value is None:
        return None
    if _LAST_EVENT_ID_PATTERN.fullmatch(value) is None:
        raise InvalidEventCursor
    parsed = int(value)
    if parsed > _MAX_EVENT_ID:
        raise InvalidEventCursor
    return parsed


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _latest_event_id(session_factory: SessionFactory) -> int:
    with session_factory() as session:
        return int(session.scalar(select(func.max(OutboxEvent.id))) or 0)


def _events_after(
    session_factory: SessionFactory, after_id: int, batch_size: int
) -> tuple[EventEnvelope, ...]:
    with session_factory() as session:
        events = session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.id > after_id)
            .order_by(OutboxEvent.id.asc())
            .limit(batch_size)
        ).all()
        envelopes: list[EventEnvelope] = []
        for event in events:
            event_type = (
                event.event_type
                if event.event_type in _EVENT_TYPES
                else "event.unknown"
            )
            aggregate_type = (
                event.aggregate_type
                if event.aggregate_type in _AGGREGATE_TYPES
                else "unknown"
            )
            envelopes.append(
                EventEnvelope(
                    # JSON numbers cannot preserve PostgreSQL bigint IDs in
                    # every browser. Decimal strings keep resume cursors exact.
                    id=str(event.id),
                    event_type=event_type,
                    aggregate_type=aggregate_type,
                    aggregate_id=str(event.aggregate_id),
                    request_id=event.request_id,
                    created_at=_timestamp(event.created_at),
                    payload=dict(event.payload),
                )
            )
        return tuple(envelopes)


def _encode_event(event: EventEnvelope) -> str:
    data = json.dumps(
        asdict(event), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {data}\n\n"


async def event_source(
    request: Request,
    *,
    last_event_id: int | None,
    session_factory: SessionFactory = SessionLocal,
    batch_size: int = 100,
    poll_interval: float = 0.75,
    heartbeat_interval: float = 15.0,
):
    """Yield ordered committed events, closing cleanly on disconnect or DB error."""

    if not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be between 1 and 500")
    if poll_interval <= 0 or heartbeat_interval <= 0:
        raise ValueError("stream intervals must be positive")

    request_id = request_id_from_scope(request.scope)
    failed = False
    with suppress(Exception):  # telemetry must never affect delivery
        METRICS.sse_connected()
    try:
        cursor = (
            last_event_id
            if last_event_id is not None
            else await run_in_threadpool(_latest_event_id, session_factory)
        )
        last_heartbeat = time.monotonic()
        yield "retry: 3000\n\n"

        while not await request.is_disconnected():
            events = await run_in_threadpool(
                _events_after, session_factory, cursor, batch_size
            )
            if events:
                for event in events:
                    yield _encode_event(event)
                    cursor = int(event.id)
                continue

            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_interval:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            await asyncio.sleep(poll_interval)
    except asyncio.CancelledError:
        raise
    except SQLAlchemyError:
        failed = True
        _LOGGER.warning(
            "event_stream_database_error",
            extra={
                "request_id": request_id,
                "exception_type": "SQLAlchemyError",
            },
        )
    except Exception as exc:  # pragma: no cover - defensive stream boundary
        failed = True
        _LOGGER.error(
            "event_stream_error",
            extra={
                "request_id": request_id,
                "exception_type": type(exc).__name__,
            },
        )
    finally:
        with suppress(Exception):  # telemetry must never affect delivery
            METRICS.sse_disconnected(failed=failed)


def event_stream_response(
    request: Request,
    last_event_id: str | None,
    *,
    session_factory: SessionFactory = SessionLocal,
    batch_size: int = 100,
    poll_interval: float = 0.75,
    heartbeat_interval: float = 15.0,
) -> StreamingResponse:
    """Build an at-least-once SSE response using ``Last-Event-ID`` semantics."""

    cursor = parse_last_event_id(last_event_id)
    # Preflight PostgreSQL before returning StreamingResponse. This preserves
    # an honest 503 problem response for initial unavailability; failures after
    # a 200 stream has opened are signaled by disconnect and client retry.
    latest_event_id = _latest_event_id(session_factory)
    if cursor is None:
        cursor = latest_event_id
    return StreamingResponse(
        event_source(
            request,
            last_event_id=cursor,
            session_factory=session_factory,
            batch_size=batch_size,
            poll_interval=poll_interval,
            heartbeat_interval=heartbeat_interval,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "private, no-store, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = [
    "EventEnvelope",
    "InvalidEventCursor",
    "event_source",
    "event_stream_response",
    "parse_last_event_id",
]
