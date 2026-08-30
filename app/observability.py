"""Bounded, dependency-free observability for the local LedgerLite service.

The module deliberately exposes only low-cardinality metric operations. Financial
identifiers, monetary values, arbitrary exception strings, and request paths are
never accepted as metric labels or structured-log fields.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER: Final = "X-Request-ID"
_REQUEST_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_ROUTE_PATTERN: Final = re.compile(r"/[A-Za-z0-9_./{}:-]{0,159}\Z")
_request_id_context: ContextVar[str | None] = ContextVar(
    "ledgerlite_request_id", default=None
)

_HTTP_METHODS: Final = frozenset(
    {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
)
_STATUS_CLASSES: Final = frozenset({"1xx", "2xx", "3xx", "4xx", "5xx"})
_LEDGER_OPERATIONS: Final = frozenset({"account", "deposit", "reversal", "transfer"})
_LEDGER_OUTCOMES: Final = frozenset({"committed", "failed", "rejected", "replayed"})
_IDEMPOTENCY_OUTCOMES: Final = frozenset({"conflict", "new", "replayed"})
_RECONCILIATION_OUTCOMES: Final = frozenset(
    {"completed", "failed", "ignored", "matched", "mismatched"}
)
_SSE_OUTCOMES: Final = frozenset({"connected", "disconnected", "failed"})
_SAFE_LOG_FIELDS: Final = (
    "service",
    "request_id",
    "method",
    "route",
    "operation",
    "status_class",
    "duration_ms",
    "replayed",
    "error_code",
    "exception_type",
)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StructuredLogFormatter(logging.Formatter):
    """Render an allowlisted JSON log record without exception messages."""

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, object] = {
            "timestamp": _utc_timestamp(),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        for name in _SAFE_LOG_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                document[name] = value
        return json.dumps(document, separators=(",", ":"), sort_keys=True)


def configure_logging(
    *,
    level: int = logging.INFO,
    logger_name: str = "ledgerlite",
) -> logging.Logger:
    """Configure LedgerLite's JSON logger once and leave third-party logs alone."""

    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.propagate = False
    configured = any(
        getattr(handler, "_ledgerlite_json", False) for handler in logger.handlers
    )
    if not configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredLogFormatter())
        handler._ledgerlite_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return logger


def current_request_id() -> str | None:
    """Return the correlation ID for the current request, when one exists."""

    return _request_id_context.get()


def request_id_from_scope(scope: Scope) -> str:
    """Return (or create) the safe request ID stored on an ASGI scope."""

    state = scope.setdefault("state", {})
    existing = state.get("request_id")
    if isinstance(existing, str) and _REQUEST_ID_PATTERN.fullmatch(existing):
        return existing

    supplied: str | None = None
    for raw_name, raw_value in scope.get("headers", ()):  # first header wins
        if raw_name.lower() != b"x-request-id":
            continue
        try:
            supplied = raw_value.decode("ascii")
        except UnicodeDecodeError:
            supplied = None
        break

    request_id = (
        supplied
        if supplied is not None and _REQUEST_ID_PATTERN.fullmatch(supplied)
        else str(uuid4())
    )
    state["request_id"] = request_id
    return request_id


def route_template(scope: Scope) -> str:
    """Return a registered route template, never the concrete request path."""

    route = scope.get("route")
    candidate = getattr(route, "path", None)
    if isinstance(candidate, str) and _ROUTE_PATTERN.fullmatch(candidate):
        return candidate
    return "unmatched"


def _operation_name(scope: Scope) -> str:
    route = scope.get("route")
    name = getattr(route, "name", None)
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", name):
        return name
    return "unknown"


def _replace_header(
    headers: list[tuple[bytes, bytes]], name: bytes, value: bytes
) -> None:
    lowered = name.lower()
    headers[:] = [(key, item) for key, item in headers if key.lower() != lowered]
    headers.append((name, value))


def _header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    lowered = name.lower()
    for key, value in headers:
        if key.lower() == lowered:
            return value
    return None


class RequestContextMiddleware:
    """Attach a request ID and emit one sanitized access event per response."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        service: str = "ledgerlite",
        logger: logging.Logger | None = None,
    ) -> None:
        self.app = app
        self.service = service
        self.logger = logger or logging.getLogger("ledgerlite.access")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = request_id_from_scope(scope)
        token = _request_id_context.set(request_id)
        started_at = time.perf_counter()
        response_started = False

        def log_response(status_code: int) -> None:
            duration_ms = round((time.perf_counter() - started_at) * 1_000, 3)
            state = scope.get("state", {})
            try:
                self.logger.info(
                    "http_request",
                    extra={
                        "service": self.service,
                        "request_id": request_id,
                        "method": _normalize_method(scope.get("method", "")),
                        "route": route_template(scope),
                        "operation": _operation_name(scope),
                        "status_class": _status_class(status_code),
                        "duration_ms": duration_ms,
                        "replayed": state.get("idempotent_replayed"),
                        "error_code": state.get("error_code"),
                    },
                )
            except Exception:  # pragma: no cover - telemetry is fail-open
                return

        async def send_with_context(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                replayed = _header_value(headers, b"idempotent-replayed")
                if replayed in {b"true", b"false"}:
                    scope.setdefault("state", {})["idempotent_replayed"] = (
                        replayed == b"true"
                    )
                _replace_header(
                    headers,
                    REQUEST_ID_HEADER.lower().encode("ascii"),
                    request_id.encode("ascii"),
                )
                message["headers"] = headers
                response_started = True
                log_response(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        except Exception:
            if not response_started:
                scope.setdefault("state", {}).setdefault(
                    "error_code", "unexpected_error"
                )
                log_response(500)
            raise
        finally:
            _request_id_context.reset(token)


@dataclass(slots=True)
class _Counter:
    name: str
    help_text: str
    label_names: tuple[str, ...]
    values: dict[tuple[str, ...], int] = field(default_factory=dict)


@dataclass(slots=True)
class _Gauge:
    name: str
    help_text: str
    label_names: tuple[str, ...]
    values: dict[tuple[str, ...], int] = field(default_factory=dict)


@dataclass(slots=True)
class _Histogram:
    name: str
    help_text: str
    label_names: tuple[str, ...]
    buckets: tuple[float, ...]
    bucket_counts: dict[tuple[str, ...], list[int]] = field(default_factory=dict)
    counts: dict[tuple[str, ...], int] = field(default_factory=dict)
    sums: dict[tuple[str, ...], float] = field(default_factory=dict)


class MetricsRegistry:
    """A tiny Prometheus registry with fixed metric and label dimensions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._http_requests = _Counter(
            "ledgerlite_http_requests_total",
            "HTTP responses by bounded route template and status class.",
            ("method", "route", "status_class"),
        )
        self._http_duration = _Histogram(
            "ledgerlite_http_request_duration_seconds",
            "Time from request receipt until response headers.",
            ("method", "route"),
            (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
        )
        self._ledger_operations = _Counter(
            "ledgerlite_ledger_operations_total",
            "Ledger operations by bounded operation and outcome.",
            ("operation", "outcome"),
        )
        self._idempotency = _Counter(
            "ledgerlite_idempotency_outcomes_total",
            "Idempotency decisions by bounded outcome.",
            ("outcome",),
        )
        self._reconciliation = _Counter(
            "ledgerlite_reconciliation_outcomes_total",
            "Reconciliation operations by bounded outcome.",
            ("outcome",),
        )
        self._sse_connections = _Gauge(
            "ledgerlite_sse_connections_active",
            "Currently active server-sent event connections.",
            (),
            {(): 0},
        )
        self._sse_lifecycle = _Counter(
            "ledgerlite_sse_connections_total",
            "Server-sent event connection lifecycle outcomes.",
            ("outcome",),
        )

    def observe_http(
        self, *, method: str, route: str, status_code: int, duration_seconds: float
    ) -> None:
        method_label = _normalize_method(method)
        route_label = route if _ROUTE_PATTERN.fullmatch(route) else "unmatched"
        status_label = _status_class(status_code)
        duration = max(0.0, float(duration_seconds))
        with self._lock:
            _increment_counter(
                self._http_requests, (method_label, route_label, status_label)
            )
            _observe_histogram(
                self._http_duration, (method_label, route_label), duration
            )

    def observe_ledger(self, operation: str, outcome: str) -> None:
        labels = (
            _allowed(operation, _LEDGER_OPERATIONS),
            _allowed(outcome, _LEDGER_OUTCOMES),
        )
        with self._lock:
            _increment_counter(self._ledger_operations, labels)

    def observe_idempotency(self, outcome: str) -> None:
        with self._lock:
            _increment_counter(
                self._idempotency, (_allowed(outcome, _IDEMPOTENCY_OUTCOMES),)
            )

    def observe_reconciliation(self, outcome: str) -> None:
        with self._lock:
            _increment_counter(
                self._reconciliation,
                (_allowed(outcome, _RECONCILIATION_OUTCOMES),),
            )

    def sse_connected(self) -> None:
        with self._lock:
            self._sse_connections.values[()] += 1
            _increment_counter(self._sse_lifecycle, ("connected",))

    def sse_disconnected(self, *, failed: bool = False) -> None:
        with self._lock:
            self._sse_connections.values[()] = max(
                0, self._sse_connections.values[()] - 1
            )
            _increment_counter(
                self._sse_lifecycle, ("failed" if failed else "disconnected",)
            )

    def render(self) -> str:
        with self._lock:
            counters = [
                _copy_counter(self._http_requests),
                _copy_counter(self._ledger_operations),
                _copy_counter(self._idempotency),
                _copy_counter(self._reconciliation),
                _copy_counter(self._sse_lifecycle),
            ]
            gauges = [_copy_gauge(self._sse_connections)]
            histograms = [_copy_histogram(self._http_duration)]

        lines: list[str] = []
        for counter in counters:
            lines.extend(_render_counter(counter))
        for gauge in gauges:
            lines.extend(_render_gauge(gauge))
        for histogram in histograms:
            lines.extend(_render_histogram(histogram))
        return "\n".join(lines) + "\n"


def _allowed(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else "other"


def _normalize_method(method: str) -> str:
    normalized = method.upper()
    return normalized if normalized in _HTTP_METHODS else "OTHER"


def _status_class(status_code: int) -> str:
    candidate = f"{status_code // 100}xx"
    return candidate if candidate in _STATUS_CLASSES else "other"


def _increment_counter(counter: _Counter, labels: tuple[str, ...]) -> None:
    counter.values[labels] = counter.values.get(labels, 0) + 1


def _observe_histogram(
    histogram: _Histogram, labels: tuple[str, ...], value: float
) -> None:
    counts = histogram.bucket_counts.setdefault(labels, [0 for _ in histogram.buckets])
    for index, upper_bound in enumerate(histogram.buckets):
        if value <= upper_bound:
            counts[index] += 1
    histogram.counts[labels] = histogram.counts.get(labels, 0) + 1
    histogram.sums[labels] = histogram.sums.get(labels, 0.0) + value


def _copy_counter(counter: _Counter) -> _Counter:
    return _Counter(
        counter.name, counter.help_text, counter.label_names, dict(counter.values)
    )


def _copy_gauge(gauge: _Gauge) -> _Gauge:
    return _Gauge(gauge.name, gauge.help_text, gauge.label_names, dict(gauge.values))


def _copy_histogram(histogram: _Histogram) -> _Histogram:
    return _Histogram(
        histogram.name,
        histogram.help_text,
        histogram.label_names,
        histogram.buckets,
        {labels: list(values) for labels, values in histogram.bucket_counts.items()},
        dict(histogram.counts),
        dict(histogram.sums),
    )


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(
    names: Sequence[str], values: Sequence[str], *, extra: tuple[str, str] | None = None
) -> str:
    pairs = [*zip(names, values, strict=True)]
    if extra is not None:
        pairs.append(extra)
    if not pairs:
        return ""
    rendered = ",".join(f'{name}="{_escape_label(value)}"' for name, value in pairs)
    return "{" + rendered + "}"


def _number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    return format(value, ".12g")


def _render_counter(counter: _Counter) -> list[str]:
    lines = [
        f"# HELP {counter.name} {counter.help_text}",
        f"# TYPE {counter.name} counter",
    ]
    for labels, value in sorted(counter.values.items()):
        lines.append(
            f"{counter.name}{_labels(counter.label_names, labels)} {_number(value)}"
        )
    return lines


def _render_gauge(gauge: _Gauge) -> list[str]:
    lines = [f"# HELP {gauge.name} {gauge.help_text}", f"# TYPE {gauge.name} gauge"]
    for labels, value in sorted(gauge.values.items()):
        lines.append(
            f"{gauge.name}{_labels(gauge.label_names, labels)} {_number(value)}"
        )
    return lines


def _render_histogram(histogram: _Histogram) -> list[str]:
    lines = [
        f"# HELP {histogram.name} {histogram.help_text}",
        f"# TYPE {histogram.name} histogram",
    ]
    for labels, count in sorted(histogram.counts.items()):
        bucket_counts = histogram.bucket_counts[labels]
        for upper_bound, bucket_count in zip(
            histogram.buckets, bucket_counts, strict=True
        ):
            labels_with_bound = _labels(
                histogram.label_names,
                labels,
                extra=("le", _number(upper_bound)),
            )
            lines.append(f"{histogram.name}_bucket{labels_with_bound} {bucket_count}")
        lines.append(
            f"{histogram.name}_bucket"
            f"{_labels(histogram.label_names, labels, extra=('le', '+Inf'))} {count}"
        )
        lines.append(
            f"{histogram.name}_sum{_labels(histogram.label_names, labels)} "
            f"{_number(histogram.sums[labels])}"
        )
        lines.append(
            f"{histogram.name}_count{_labels(histogram.label_names, labels)} {count}"
        )
    return lines


METRICS = MetricsRegistry()


class MetricsMiddleware:
    """Observe response-header latency without delaying or changing a request."""

    def __init__(self, app: ASGIApp, *, registry: MetricsRegistry = METRICS) -> None:
        self.app = app
        self.registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_at = time.perf_counter()
        observed = False

        def observe(status_code: int, headers: list[tuple[bytes, bytes]]) -> None:
            nonlocal observed
            if observed:
                return
            observed = True
            try:
                self.registry.observe_http(
                    method=scope.get("method", ""),
                    route=route_template(scope),
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started_at,
                )
                replayed = _header_value(headers, b"idempotent-replayed")
                if replayed == b"true":
                    self.registry.observe_idempotency("replayed")
                elif replayed == b"false":
                    self.registry.observe_idempotency("new")
                elif scope.get("state", {}).get("error_code") == (
                    "idempotency_conflict"
                ):
                    self.registry.observe_idempotency("conflict")

                operation = _ledger_operation(scope)
                if operation is not None:
                    if replayed == b"true":
                        outcome = "replayed"
                    elif 200 <= status_code < 300:
                        outcome = "committed"
                    elif 400 <= status_code < 500:
                        outcome = "rejected"
                    else:
                        outcome = "failed"
                    self.registry.observe_ledger(operation, outcome)
            except Exception:  # pragma: no cover - telemetry is fail-open
                return

        async def send_with_metrics(message: Message) -> None:
            if message["type"] == "http.response.start":
                observe(message["status"], list(message.get("headers", ())))
            await send(message)

        try:
            await self.app(scope, receive, send_with_metrics)
        except Exception:
            observe(500, [])
            raise


def _ledger_operation(scope: Scope) -> str | None:
    if scope.get("method") != "POST":
        return None
    template = route_template(scope)
    if template.endswith("/deposits"):
        return "deposit"
    if template.endswith("/reversals"):
        return "reversal"
    if template in {"/api/v1/transfers", "/transfers"}:
        return "transfer"
    if template in {"/api/v1/accounts", "/accounts"}:
        return "account"
    return None


def metrics_response(*, registry: MetricsRegistry = METRICS) -> Response:
    """Return the registry using Prometheus' stable text exposition format."""

    return Response(
        registry.render(),
        headers={
            "Content-Type": "text/plain; version=0.0.4; charset=utf-8",
            "Cache-Control": "private, no-store",
        },
    )


def observe_ledger_operation(operation: str, outcome: str) -> None:
    try:
        METRICS.observe_ledger(operation, outcome)
    except Exception:  # pragma: no cover - telemetry is fail-open
        return


def observe_idempotency(outcome: str) -> None:
    try:
        METRICS.observe_idempotency(outcome)
    except Exception:  # pragma: no cover - telemetry is fail-open
        return


def observe_reconciliation(outcome: str) -> None:
    try:
        METRICS.observe_reconciliation(outcome)
    except Exception:  # pragma: no cover - telemetry is fail-open
        return


__all__ = [
    "METRICS",
    "MetricsMiddleware",
    "MetricsRegistry",
    "REQUEST_ID_HEADER",
    "RequestContextMiddleware",
    "StructuredLogFormatter",
    "configure_logging",
    "current_request_id",
    "metrics_response",
    "observe_idempotency",
    "observe_ledger_operation",
    "observe_reconciliation",
    "request_id_from_scope",
    "route_template",
]
