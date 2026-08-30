"""Sanitized RFC 9457-style problem responses and FastAPI handlers."""

from __future__ import annotations

import logging
import re
from http import HTTPStatus
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import (
    DisconnectionError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import LedgerError
from app.observability import REQUEST_ID_HEADER, request_id_from_scope

_ERROR_CODE_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
_DOMAIN_CODES: Final = {
    "account not found": "account_not_found",
    "accounts must have the same currency": "currency_mismatch",
    "amount exceeds NUMERIC(20, 2)": "amount_out_of_range",
    "amount is not a valid decimal": "invalid_amount",
    "amount must be a finite decimal": "invalid_amount",
    "amount must be greater than zero": "invalid_amount",
    "amount must have at most 2 decimals": "invalid_amount",
    "cursor does not match the active filters": "cursor_filter_mismatch",
    "cursor does not match this resource": "cursor_resource_mismatch",
    "cursor exceeds the maximum length": "cursor_too_long",
    "cursor is malformed": "invalid_cursor",
    "cursor version is unsupported": "unsupported_cursor_version",
    "database unavailable": "database_unavailable",
    "display name must be 1 to 80 characters": "invalid_display_name",
    "display name must be text": "invalid_display_name",
    "idempotency key must contain visible ASCII only": "invalid_idempotency_key",
    "idempotency key was already used for another request": "idempotency_conflict",
    "insufficient funds": "insufficient_funds",
    "insufficient funds to reverse transaction": "insufficient_reversal_funds",
    "invalid reversal reason code": "invalid_reversal_reason",
    "only transfers and reversals can be reconciled": (
        "transaction_type_not_reconcilable"
    ),
    "provider item or transaction is outside the reconciliation period": (
        "reconciliation_outside_period"
    ),
    "reconciliation item is already resolved": "reconciliation_item_resolved",
    "reconciliation item not found": "reconciliation_item_not_found",
    "reconciliation result is invalid": "invalid_reconciliation_result",
    "reconciliation resolution status is invalid": (
        "invalid_reconciliation_resolution"
    ),
    "reconciliation run cannot be executed": "reconciliation_run_conflict",
    "reconciliation run has not completed": "reconciliation_run_pending",
    "reconciliation run not found": "reconciliation_run_not_found",
    "reversal note must be 1 to 240 trimmed characters": "invalid_reversal_note",
    "reversals cannot be reversed": "reversal_of_reversal",
    "source and destination accounts must differ": "same_account_transfer",
    "transaction amount and currency must match the provider item": (
        "reconciliation_match_mismatch"
    ),
    "transaction and provider item must use the run currency": (
        "reconciliation_currency_mismatch"
    ),
    "transaction is already matched in this run": "transaction_already_matched",
    "transaction is outside the reconciliation period": "transaction_outside_period",
    "transaction not found": "transaction_not_found",
    "transaction was already reversed": "transaction_already_reversed",
    "ledger-only items cannot be manually matched": "ledger_only_match_forbidden",
    "the corresponding ledger-only exception is already resolved": (
        "ledger_only_already_resolved"
    ),
    "a resolution reason is required": "resolution_reason_required",
    "resolution note must not exceed 240 characters": "resolution_note_too_long",
}
_STATUS_DETAILS: Final = {
    400: "The request could not be processed.",
    401: "Authentication is required.",
    403: "The request is not permitted.",
    404: "The requested resource was not found.",
    405: "The HTTP method is not allowed for this resource.",
    409: "The request conflicts with the current resource state.",
    413: "The request is too large.",
    415: "The request media type is not supported.",
    422: "Request validation failed.",
    429: "Too many requests were received.",
    500: "An unexpected error occurred.",
    503: "The service is temporarily unavailable.",
}


def _title(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Request Error"


def _default_detail(status_code: int) -> str:
    return _STATUS_DETAILS.get(status_code, "The request could not be processed.")


def _error_code(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "authentication_required",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "request_too_large",
        415: "unsupported_media_type",
        422: "validation_error",
        429: "rate_limited",
        503: "service_unavailable",
    }.get(status_code, "internal_error" if status_code >= 500 else "request_error")


def _instance(request_id: str) -> str:
    return f"urn:ledgerlite:request:{request_id}"


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    detail: str,
    title: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a safe problem document and attach its request correlation ID."""

    if not 400 <= status_code <= 599:
        status_code = 500
        code = "internal_error"
        detail = _default_detail(500)
    if _ERROR_CODE_PATTERN.fullmatch(code) is None:
        code = _error_code(status_code)

    request_id = request_id_from_scope(request.scope)
    request.scope.setdefault("state", {})["error_code"] = code
    response_headers = dict(headers or {})
    response_headers.update(
        {
            REQUEST_ID_HEADER: request_id,
            "Cache-Control": "private, no-store, no-transform",
            "Content-Security-Policy": (
                "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            ),
            "Permissions-Policy": (
                "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }
    )
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        headers=response_headers,
        content={
            "type": f"urn:ledgerlite:error:{code}",
            "title": title or _title(status_code),
            "status": status_code,
            "code": code,
            "detail": detail,
            "instance": _instance(request_id),
            "request_id": request_id,
        },
    )


async def ledger_error_handler(request: Request, exc: LedgerError) -> JSONResponse:
    status_code = exc.status_code if 400 <= exc.status_code <= 599 else 500
    explicit_code = getattr(exc, "code", None)
    code = (
        explicit_code
        if isinstance(explicit_code, str)
        and _ERROR_CODE_PATTERN.fullmatch(explicit_code)
        else _DOMAIN_CODES.get(exc.detail, _error_code(status_code))
    )
    detail = exc.detail if status_code < 500 else _default_detail(status_code)
    headers = {"Retry-After": "1"} if status_code == 503 else None
    return problem_response(
        request,
        status_code=status_code,
        code=code,
        detail=detail,
        headers=headers,
    )


async def validation_error_handler(
    request: Request, _exc: RequestValidationError
) -> JSONResponse:
    # Pydantic errors intentionally are not returned: they may include submitted input.
    return problem_response(
        request,
        status_code=422,
        code="validation_error",
        detail=_default_detail(422),
    )


async def http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    status_code = exc.status_code if 400 <= exc.status_code <= 599 else 500
    return problem_response(
        request,
        status_code=status_code,
        code=_error_code(status_code),
        detail=_default_detail(status_code),
        headers=dict(exc.headers or {}),
    )


async def database_error_handler(
    request: Request, exc: SQLAlchemyError
) -> JSONResponse:
    transient = isinstance(
        exc,
        (
            DisconnectionError,
            InterfaceError,
            OperationalError,
            SQLAlchemyTimeoutError,
        ),
    )
    if not transient:
        return problem_response(
            request,
            status_code=500,
            code="internal_error",
            detail=_default_detail(500),
        )
    return problem_response(
        request,
        status_code=503,
        code="database_unavailable",
        detail=_default_detail(503),
        headers={"Retry-After": "1"},
    )


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Do not stringify the exception: driver errors can contain SQL parameters.
    logging.getLogger("ledgerlite.errors").error(
        "unhandled_exception",
        extra={
            "request_id": request_id_from_scope(request.scope),
            "exception_type": type(exc).__name__,
        },
    )
    return problem_response(
        request,
        status_code=500,
        code="internal_error",
        detail=_default_detail(500),
    )


def register_problem_handlers(app: FastAPI) -> None:
    """Install the complete sanitized error boundary on a FastAPI app."""

    app.add_exception_handler(LedgerError, ledger_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(  # type: ignore[arg-type]
        RequestValidationError, validation_error_handler
    )
    app.add_exception_handler(StarletteHTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(SQLAlchemyError, database_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unexpected_error_handler)


__all__ = [
    "database_error_handler",
    "http_error_handler",
    "ledger_error_handler",
    "problem_response",
    "register_problem_handlers",
    "unexpected_error_handler",
    "validation_error_handler",
]
