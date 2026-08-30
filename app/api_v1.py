"""Versioned product API for the LedgerLite operations console.

The unversioned compatibility routes remain in :mod:`app.main`.  This router is
the stable surface used by the console and keeps HTTP concerns out of the
ledger, query, and reconciliation services.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import events, queries, reconciliation, services
from app.api_schemas import (
    AccountCreate,
    AccountResponse,
    AccountsPage,
    ConsoleCapabilities,
    DepositCreate,
    DepositResponse,
    OverviewResponse,
    ReconciliationIgnoreRequest,
    ReconciliationItemResponse,
    ReconciliationItemsPage,
    ReconciliationMatchRequest,
    ReconciliationRunResponse,
    ReconciliationRunsResponse,
    ReversalCreate,
    ReversalResponse,
    StatementPage,
    TransactionDetail,
    TransactionsPage,
    TransferCreate,
    TransferResponse,
    problem_response_spec,
)
from app.config import Settings, get_settings
from app.database import get_session
from app.ledger import reverse_transaction
from app.observability import observe_reconciliation

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^[!-~]+$",
        description=(
            "Visible-ASCII client key. Exact retries replay the original result; "
            "reuse for another request returns a conflict."
        ),
    ),
]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageCursor = Annotated[str | None, Query(max_length=2048)]
CurrencyFilter = Annotated[
    str | None,
    Query(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$"),
]
TransactionType = Literal["deposit", "transfer", "reversal"]
ReconciliationResult = Literal[
    "pending",
    "matched",
    "provider_only",
    "ledger_only",
    "mismatched",
    "duplicate",
]
ResolutionStatus = Literal["open", "matched", "ignored"]

IDEMPOTENT_CREATED = {
    "description": "Created once or replayed from the original committed response.",
    "headers": {
        "Idempotent-Replayed": {
            "description": "True only when the original response was replayed.",
            "schema": {"type": "boolean"},
        }
    },
}
NOT_FOUND = problem_response_spec("The requested ledger resource does not exist.")
CONFLICT = problem_response_spec(
    "The request conflicts with funds, idempotency, reversal, or reconciliation state."
)
VALIDATION = problem_response_spec("The request did not match the API contract.")
UNAVAILABLE = problem_response_spec(
    "PostgreSQL is temporarily unavailable.", retryable=True
)
EVENT_STREAM_OK = {
    "description": "Committed events in resumable server-sent event frames.",
    "content": {
        "text/event-stream": {
            "schema": {"type": "string"},
            "example": (
                "retry: 3000\n\n"
                "id: 42\n"
                "event: posting.created\n"
                'data: {"id":"42","event_type":"posting.created",'
                '"aggregate_type":"ledger_transaction",'
                '"aggregate_id":"00000000-0000-4000-8000-000000000042",'
                '"request_id":"request-42",'
                '"created_at":"2026-08-26T12:00:00.000Z",'
                '"payload":{"operation":"transfer"}}\n\n'
            ),
        }
    },
}

router = APIRouter(
    prefix="/api/v1",
    responses={422: VALIDATION, 503: UNAVAILABLE},
)


def _request_id(request: Request) -> str | None:
    """Read the correlation ID installed by operational middleware, if any."""

    value = getattr(request.state, "request_id", None)
    return str(value) if value is not None else None


@router.get(
    "/capabilities",
    response_model=ConsoleCapabilities,
    tags=["operations"],
    summary="Discover optional console links",
)
def console_capabilities(
    runtime_settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return {
        "documentation": runtime_settings.app_env != "production",
    }


@router.get(
    "/overview",
    response_model=OverviewResponse,
    tags=["overview"],
    summary="Summarize balances and journal activity",
)
def overview(
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    """Return one database-snapshot view of balances and journal state."""

    return queries.get_overview(session)


@router.get(
    "/accounts",
    response_model=AccountsPage,
    tags=["accounts"],
    summary="List customer accounts",
)
def list_accounts(
    session: Annotated[Session, Depends(get_session)],
    currency: CurrencyFilter = None,
    limit: PageLimit = 25,
    cursor: PageCursor = None,
) -> dict[str, object]:
    return queries.list_accounts(
        session,
        currency=currency,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "/accounts",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["accounts"],
    summary="Create a customer account",
    responses={201: IDEMPOTENT_CREATED, 409: CONFLICT},
)
def create_account(
    body: AccountCreate,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: IdempotencyKey,
) -> dict[str, object]:
    result = services.create_account(
        session,
        body.currency,
        display_name=body.display_name,
        idempotency_key=idempotency_key,
    )
    response.headers["Idempotent-Replayed"] = str(result.replayed).lower()
    return result.payload


@router.get(
    "/accounts/{account_id}/statement",
    response_model=StatementPage,
    tags=["accounts"],
    summary="Read a keyset-paginated account statement",
    responses={404: NOT_FOUND},
)
def account_statement(
    account_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    limit: PageLimit = 25,
    cursor: PageCursor = None,
) -> dict[str, object]:
    return queries.get_account_statement(
        session,
        account_id,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "/accounts/{account_id}/deposits",
    response_model=DepositResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["accounts"],
    summary="Post a deposit",
    responses={201: IDEMPOTENT_CREATED, 404: NOT_FOUND, 409: CONFLICT},
)
def deposit(
    account_id: UUID,
    body: DepositCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: IdempotencyKey,
) -> dict[str, object]:
    result = services.deposit(
        session,
        account_id,
        body.amount,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
    )
    response.headers["Idempotent-Replayed"] = str(result.replayed).lower()
    return result.payload


@router.post(
    "/transfers",
    response_model=TransferResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["transactions"],
    summary="Post a transfer",
    responses={201: IDEMPOTENT_CREATED, 404: NOT_FOUND, 409: CONFLICT},
)
def transfer(
    body: TransferCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: IdempotencyKey,
) -> dict[str, object]:
    result = services.transfer(
        session,
        body.source_account_id,
        body.destination_account_id,
        body.amount,
        idempotency_key,
        request_id=_request_id(request),
    )
    response.headers["Idempotent-Replayed"] = str(result.replayed).lower()
    return result.payload


@router.get(
    "/transactions",
    response_model=TransactionsPage,
    tags=["transactions"],
    summary="Explore ledger transactions",
)
def list_transactions(
    session: Annotated[Session, Depends(get_session)],
    currency: CurrencyFilter = None,
    transaction_type: Annotated[
        TransactionType | None,
        Query(alias="type"),
    ] = None,
    account_id: UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: PageLimit = 25,
    cursor: PageCursor = None,
) -> dict[str, object]:
    return queries.list_transactions(
        session,
        currency=currency,
        transaction_type=transaction_type,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/transactions/{transaction_id}",
    response_model=TransactionDetail,
    tags=["transactions"],
    summary="Inspect a transaction",
    responses={404: NOT_FOUND},
)
def transaction_detail(
    transaction_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    return queries.get_transaction(session, transaction_id)


@router.post(
    "/transactions/{transaction_id}/reversals",
    response_model=ReversalResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["transactions"],
    summary="Create a compensating reversal",
    responses={201: IDEMPOTENT_CREATED, 404: NOT_FOUND, 409: CONFLICT},
)
def reverse(
    transaction_id: UUID,
    body: ReversalCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: IdempotencyKey,
) -> dict[str, object]:
    result = reverse_transaction(
        session,
        transaction_id,
        idempotency_key,
        body.reason_code,
        note=body.note,
        request_id=_request_id(request),
    )
    response.headers["Idempotent-Replayed"] = str(result.replayed).lower()
    return result.payload


@router.get(
    "/reconciliation/runs",
    response_model=ReconciliationRunsResponse,
    tags=["reconciliation"],
    summary="List reconciliation runs",
)
def reconciliation_runs(
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    return reconciliation.list_runs(session)


@router.get(
    "/reconciliation/runs/{run_id}",
    response_model=ReconciliationRunResponse,
    tags=["reconciliation"],
    summary="Inspect a reconciliation run",
    responses={404: NOT_FOUND},
)
def reconciliation_run(
    run_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    return reconciliation.get_run(session, run_id)


@router.post(
    "/reconciliation/runs/{run_id}/execute",
    response_model=ReconciliationRunResponse,
    tags=["reconciliation"],
    summary="Run reconciliation",
    responses={404: NOT_FOUND, 409: CONFLICT},
)
def execute_reconciliation_run(
    run_id: UUID,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    try:
        result = reconciliation.execute_run(
            session,
            run_id,
            request_id=_request_id(request),
        )
    except Exception:
        observe_reconciliation("failed")
        raise
    observe_reconciliation("completed")
    return result


@router.get(
    "/reconciliation/runs/{run_id}/items",
    response_model=ReconciliationItemsPage,
    tags=["reconciliation"],
    summary="List reconciliation exceptions and matches",
    responses={404: NOT_FOUND},
)
def reconciliation_items(
    run_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    result: ReconciliationResult | None = None,
    resolution_status: ResolutionStatus | None = None,
    limit: PageLimit = 25,
    cursor: PageCursor = None,
) -> dict[str, object]:
    return reconciliation.list_items(
        session,
        run_id,
        result=result,
        resolution_status=resolution_status,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "/reconciliation/items/{item_id}/match",
    response_model=ReconciliationItemResponse,
    tags=["reconciliation"],
    summary="Manually match a compatible ledger transaction",
    responses={404: NOT_FOUND, 409: CONFLICT},
)
def match_reconciliation_item(
    item_id: UUID,
    body: ReconciliationMatchRequest,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    try:
        result = reconciliation.match_item(
            session,
            item_id,
            body.transaction_id,
            request_id=_request_id(request),
            note=body.note,
        )
    except Exception:
        observe_reconciliation("failed")
        raise
    observe_reconciliation("matched")
    return result


@router.post(
    "/reconciliation/items/{item_id}/ignore",
    response_model=ReconciliationItemResponse,
    tags=["reconciliation"],
    summary="Resolve an exception with a bounded reason",
    responses={404: NOT_FOUND, 409: CONFLICT},
)
def ignore_reconciliation_item(
    item_id: UUID,
    body: ReconciliationIgnoreRequest,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    try:
        result = reconciliation.ignore_item(
            session,
            item_id,
            body.reason,
            request_id=_request_id(request),
        )
    except Exception:
        observe_reconciliation("failed")
        raise
    observe_reconciliation("ignored")
    return result


@router.get(
    "/events/stream",
    response_class=StreamingResponse,
    tags=["events"],
    summary="Stream committed ledger and reconciliation events",
    responses={200: EVENT_STREAM_OK},
)
def stream_events(
    request: Request,
    last_event_id: Annotated[
        str | None,
        Header(alias="Last-Event-ID", max_length=20),
    ] = None,
) -> StreamingResponse:
    """Resume an at-least-once server-sent event stream after an event ID."""

    return events.event_stream_response(request, last_event_id)


__all__ = ["router"]
