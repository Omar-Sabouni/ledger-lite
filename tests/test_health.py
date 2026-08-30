from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.requests import Request

from app import events
from app.errors import LedgerError
from app.main import health
from app.observability import MetricsRegistry, metrics_response
from app.problem_details import (
    database_error_handler,
    http_error_handler,
    ledger_error_handler,
    problem_response,
    unexpected_error_handler,
    validation_error_handler,
)


class UnavailableSession:
    def scalar(self, _statement: object) -> None:
        raise OperationalError("SELECT 1", {}, RuntimeError("database is down"))


def test_health_maps_database_failures_to_service_unavailable() -> None:
    with pytest.raises(LedgerError) as raised:
        health(UnavailableSession())  # type: ignore[arg-type]

    assert raised.value.status_code == 503
    assert raised.value.detail == "database unavailable"


def _request(request_id: bytes = b"unit-request") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/unit",
            "headers": [(b"x-request-id", request_id)],
            "state": {},
        }
    )


def _body(response: object) -> dict[str, Any]:
    return json.loads(response.body)  # type: ignore[attr-defined]


def test_problem_handlers_return_sanitized_correlated_contracts() -> None:
    invalid = problem_response(
        _request(), status_code=200, code="INVALID", detail="secret"
    )
    assert invalid.status_code == 500
    assert _body(invalid)["code"] == "internal_error"
    assert "secret" not in invalid.body.decode()

    async def exercise() -> list[object]:
        return [
            await ledger_error_handler(
                _request(), LedgerError(409, "insufficient funds")
            ),
            await ledger_error_handler(
                _request(), LedgerError(503, "driver secret", code="db_down")
            ),
            await validation_error_handler(_request(), RequestValidationError([])),
            await http_error_handler(
                _request(), HTTPException(405, headers={"Allow": "GET"})
            ),
            await database_error_handler(
                _request(), OperationalError("SELECT secret", {}, RuntimeError())
            ),
            await database_error_handler(_request(), SQLAlchemyError("secret")),
            await unexpected_error_handler(_request(), RuntimeError("secret")),
        ]

    handled = asyncio.run(exercise())
    codes = [_body(response)["code"] for response in handled]
    assert codes == [
        "insufficient_funds",
        "db_down",
        "validation_error",
        "method_not_allowed",
        "database_unavailable",
        "internal_error",
        "internal_error",
    ]
    assert handled[1].headers["retry-after"] == "1"
    assert handled[3].headers["allow"] == "GET"
    assert all("secret" not in response.body.decode() for response in handled[1:])


class _EventSession:
    def __init__(self, latest: int = 0, rows: tuple[object, ...] = ()) -> None:
        self.latest = latest
        self.rows = rows

    def __enter__(self) -> _EventSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def scalar(self, _statement: object) -> int:
        return self.latest

    def scalars(self, _statement: object) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self.rows))


class _Disconnect:
    def __init__(self, states: list[bool]) -> None:
        self.scope = {
            "type": "http",
            "headers": [(b"x-request-id", b"stream-unit")],
            "state": {},
        }
        self.states = iter(states)

    async def is_disconnected(self) -> bool:
        return next(self.states, True)


def test_event_delivery_is_allowlisted_resumable_and_heartbeat_driven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert events.parse_last_event_id(None) is None
    assert events.parse_last_event_id("9223372036854775807") == 2**63 - 1
    for malformed in ("", " 1", "01", "9223372036854775808"):
        with pytest.raises(events.InvalidEventCursor):
            events.parse_last_event_id(malformed)

    row = SimpleNamespace(
        id=9_007_199_254_740_993,
        event_type="unsafe.custom",
        aggregate_type="unsafe.aggregate",
        aggregate_id=uuid4(),
        request_id=None,
        created_at=datetime(2026, 1, 2, 3, 4, 5, 678000),
        payload={"amount": "10.00"},
    )

    def factory() -> _EventSession:
        return _EventSession(row.id, (row,))

    envelope = events._events_after(factory, 0, 100)[0]
    assert events._latest_event_id(factory) == row.id
    assert envelope.id == "9007199254740993"
    assert envelope.event_type == "event.unknown"
    assert envelope.aggregate_type == "unknown"
    assert envelope.created_at == "2026-01-02T03:04:05.678Z"

    delivered = events.EventEnvelope(
        id="6",
        event_type="posting.created",
        aggregate_type="ledger_transaction",
        aggregate_id=str(uuid4()),
        request_id=None,
        created_at="2026-01-01T00:00:00.000Z",
        payload={},
    )
    batches = iter([(delivered,), ()])
    clock = iter([1.0, 3.0])

    async def inline(function: object, *args: object) -> object:
        return function(*args)  # type: ignore[operator]

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(events, "run_in_threadpool", inline)
    monkeypatch.setattr(events, "_latest_event_id", lambda _factory: 5)
    monkeypatch.setattr(events, "_events_after", lambda *_args: next(batches))
    monkeypatch.setattr(events, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(events.asyncio, "sleep", no_sleep)

    async def collect() -> list[str]:
        return [
            item
            async for item in events.event_source(
                _Disconnect([False, False, True]),  # type: ignore[arg-type]
                last_event_id=None,
                heartbeat_interval=1,
                poll_interval=0.001,
            )
        ]

    streamed = asyncio.run(collect())
    assert streamed[0] == "retry: 3000\n\n"
    assert streamed[1].startswith("id: 6\nevent: posting.created\n")
    assert streamed[2] == ": heartbeat\n\n"

    def fail(*_args: object) -> tuple[object, ...]:
        raise SQLAlchemyError("database down")

    monkeypatch.setattr(events, "_events_after", fail)
    monkeypatch.setattr(events, "time", SimpleNamespace(monotonic=lambda: 1.0))

    async def collect_failure() -> list[str]:
        return [
            item
            async for item in events.event_source(
                _Disconnect([False]),  # type: ignore[arg-type]
                last_event_id=0,
                poll_interval=0.001,
            )
        ]

    assert asyncio.run(collect_failure()) == ["retry: 3000\n\n"]
    response = events.event_stream_response(_request(), None, session_factory=factory)
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "private, no-store, no-transform"


def test_metrics_and_logs_expose_only_bounded_operational_evidence() -> None:
    registry = MetricsRegistry()
    registry.observe_http(
        method="post", route="/api/v1/transfers", status_code=201, duration_seconds=0.01
    )
    registry.observe_http(
        method="TRACE", route="unsafe", status_code=799, duration_seconds=-1
    )
    registry.observe_ledger("secret", "strange")
    registry.observe_idempotency("replayed")
    registry.observe_reconciliation("matched")
    registry.sse_connected()
    registry.sse_disconnected(failed=True)
    rendered = registry.render()
    assert 'method="POST",route="/api/v1/transfers",status_class="2xx"' in rendered
    assert 'operation="other",outcome="other"' in rendered
    assert "ledgerlite_sse_connections_active 0" in rendered
    assert 'le="+Inf"' in rendered
    assert metrics_response(registry=registry).headers["cache-control"] == (
        "private, no-store"
    )
