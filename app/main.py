"""LedgerLite HTTP composition: console, stable API, and operational surfaces."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.routing import Match

from app import services
from app.api_schemas import problem_response_spec
from app.api_v1 import router as api_v1_router
from app.config import get_settings
from app.database import get_session
from app.errors import LedgerError
from app.observability import (
    MetricsMiddleware,
    RequestContextMiddleware,
    configure_logging,
    metrics_response,
)
from app.problem_details import register_problem_handlers
from app.schemas import (
    AccountCreate,
    AccountResponse,
    DepositCreate,
    DepositResponse,
    HealthResponse,
    StatementResponse,
    TransferCreate,
    TransferResponse,
)
from app.security_headers import SecurityHeadersMiddleware

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
FRONTEND_DIST_CANDIDATES = (
    Path.cwd() / "frontend" / "dist",
    PROJECT_ROOT / "frontend" / "dist",
)
settings = get_settings()
docs_enabled = settings.app_env != "production"

logger = configure_logging(level=getattr(logging, settings.log_level))

app = FastAPI(
    title="LedgerLite",
    version="unversioned",
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json" if docs_enabled else None,
    summary="Local double-entry ledger and reconciliation API.",
    description=(
        "LedgerLite is a local AED console for double-entry posting, "
        "compensating reversals, and processor reconciliation. Amounts cross "
        "the API as decimal strings and balances are calculated from signed "
        "postings.\n\n"
        "**Boundary:** this application has no "
        "authentication, authorization, tenancy, or account ownership. It is "
        "bound to loopback by Compose and must not be exposed publicly or used "
        "with real money or sensitive data."
    ),
    openapi_tags=[
        {"name": "overview", "description": "Balance totals and journal activity."},
        {"name": "accounts", "description": "Accounts, deposits, and statements."},
        {
            "name": "transactions",
            "description": "Transfers, journal details, and compensating reversals.",
        },
        {
            "name": "reconciliation",
            "description": "Processor matching and exception resolution.",
        },
        {"name": "events", "description": "Committed transactional-outbox events."},
        {"name": "operations", "description": "Service state and metrics."},
    ],
)

# Middleware is registered inside-out so correlation/logging is the outermost
# layer, metrics observes the final response, and every response gets browser
# security and no-store policy headers.
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MetricsMiddleware)
app.add_middleware(
    RequestContextMiddleware,
    service="ledgerlite",
    logger=logger,
)
register_problem_handlers(app)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="documentation-static")
app.include_router(api_v1_router)
api_routers = [api_v1_router]


def _docs_navigation(active_view: str) -> str:
    links = (
        ("swagger", "/docs", "Swagger UI"),
        ("redoc", "/redoc", "ReDoc"),
        ("schema", "/openapi.json", "OpenAPI JSON"),
    )
    navigation = []
    for view, href, label in links:
        current = ' aria-current="page"' if view == active_view else ""
        navigation.append(f'<a href="{href}"{current}>{label}</a>')
    return "".join(navigation)


def _documentation_shell(html: str, active_view: str) -> str:
    """Add one safety notice and a format switcher around generated docs."""

    body_start = f"""<body class="docs-page docs-page--{active_view}">
  <a class="docs-skip-link" href="#api-reference">Skip to API reference</a>
  <header class="docs-toolbar">
    <p class="docs-warning">
      <strong>Local ledger console — no authentication or ownership checks.</strong>
      Do not expose publicly or use real money or sensitive data.
    </p>
    <nav class="docs-formats" aria-label="API documentation formats">
      {_docs_navigation(active_view)}
    </nav>
  </header>
  <main id="api-reference" tabindex="-1">
"""
    body_end = """  </main>
</body>"""

    if "<body>" not in html:
        raise RuntimeError("generated documentation HTML has no body element")
    return (
        html.replace("<html>", '<html lang="en">', 1)
        .replace("<body>", body_start, 1)
        .replace("</body>", body_end, 1)
    )


def swagger_docs() -> HTMLResponse:
    """Serve native Swagger UI with the sandbox boundary in one shared shell."""

    openapi_url = app.openapi_url
    if openapi_url is None:
        raise LedgerError(404, "API documentation is disabled", code="not_found")
    response = get_swagger_ui_html(
        openapi_url=openapi_url,
        title="LedgerLite · API reference",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_ui_parameters={
            "deepLinking": True,
            "displayRequestDuration": True,
            "docExpansion": "list",
            "filter": True,
            "persistAuthorization": False,
            "tryItOutEnabled": True,
        },
    )
    html = response.body.decode("utf-8").replace(
        "</head>",
        '<meta name="theme-color" content="#111820">'
        '<link rel="stylesheet" href="/static/ledger-docs.css">'
        "</head>",
    )
    return HTMLResponse(_documentation_shell(html, "swagger"))


def redoc_docs() -> HTMLResponse:
    """Serve native ReDoc using the current schema."""

    openapi_url = app.openapi_url
    if openapi_url is None:
        raise LedgerError(404, "API documentation is disabled", code="not_found")
    response = get_redoc_html(
        openapi_url=openapi_url,
        title="LedgerLite · API reference",
        with_google_fonts=False,
    )
    html = response.body.decode("utf-8").replace(
        "</head>",
        '<meta name="theme-color" content="#111820">'
        '<link rel="stylesheet" href="/static/ledger-docs.css">'
        "</head>",
    )
    return HTMLResponse(_documentation_shell(html, "redoc"))


def swagger_ui_redirect() -> HTMLResponse:
    return get_swagger_ui_oauth2_redirect_html()


if docs_enabled:
    app.add_api_route("/docs", swagger_docs, include_in_schema=False)
    app.add_api_route("/redoc", redoc_docs, include_in_schema=False)
    app.add_api_route(
        "/docs/oauth2-redirect",
        swagger_ui_redirect,
        include_in_schema=False,
    )


def _database_readiness(session: Session) -> dict[str, str]:
    try:
        ready = session.scalar(
            text(
                """
                WITH required_schema AS (
                    SELECT
                        ROW(
                            account.id,
                            account.currency,
                            account.is_system,
                            account.system_key,
                            account.display_name,
                            account.created_at
                        ) AS account_shape,
                        ROW(
                            transaction.id,
                            transaction.type,
                            transaction.amount,
                            transaction.currency,
                            transaction.source_account_id,
                            transaction.destination_account_id,
                            transaction.idempotency_key,
                            transaction.request_fingerprint,
                            transaction.response_payload,
                            transaction.reverses_transaction_id,
                            transaction.reversal_reason_code,
                            transaction.reversal_note,
                            transaction.posting_sequence,
                            transaction.created_at
                        ) AS transaction_shape,
                        ROW(
                            entry.id,
                            entry.transaction_id,
                            entry.account_id,
                            entry.sequence,
                            entry.amount,
                            entry.currency,
                            entry.created_at
                        ) AS entry_shape,
                        ROW(
                            event.id,
                            event.event_type,
                            event.aggregate_type,
                            event.aggregate_id,
                            event.request_id,
                            event.payload,
                            event.created_at
                        ) AS event_shape,
                        ROW(
                            run.id,
                            run.provider,
                            run.fixture_key,
                            run.currency,
                            run.period_start,
                            run.period_end,
                            run.status,
                            run.summary,
                            run.created_at,
                            run.completed_at
                        ) AS run_shape,
                        ROW(
                            item.id,
                            item.run_id,
                            item.provider_reference,
                            item.claimed_transaction_id,
                            item.matched_transaction_id,
                            item.amount,
                            item.currency,
                            item.occurred_at,
                            item.result,
                            item.mismatch_code,
                            item.resolution_status,
                            item.resolution_note,
                            item.created_at,
                            item.resolved_at
                        ) AS item_shape
                    FROM accounts AS account
                    CROSS JOIN ledger_transactions AS transaction
                    CROSS JOIN ledger_entries AS entry
                    CROSS JOIN outbox_events AS event
                    CROSS JOIN reconciliation_runs AS run
                    CROSS JOIN reconciliation_items AS item
                    WHERE false
                ),
                required_column_privileges(
                    relation_name,
                    column_name,
                    privilege_name
                ) AS (
                    VALUES
                        ('public.ledger_transactions', 'id', 'INSERT'),
                        ('public.ledger_transactions', 'type', 'INSERT'),
                        ('public.ledger_transactions', 'amount', 'INSERT'),
                        ('public.ledger_transactions', 'currency', 'INSERT'),
                        (
                            'public.ledger_transactions',
                            'source_account_id',
                            'INSERT'
                        ),
                        (
                            'public.ledger_transactions',
                            'destination_account_id',
                            'INSERT'
                        ),
                        (
                            'public.ledger_transactions',
                            'idempotency_key',
                            'INSERT'
                        ),
                        (
                            'public.ledger_transactions',
                            'request_fingerprint',
                            'INSERT'
                        ),
                        (
                            'public.ledger_transactions',
                            'response_payload',
                            'INSERT'
                        ),
                        (
                            'public.ledger_transactions',
                            'reverses_transaction_id',
                            'INSERT'
                        ),
                        (
                            'public.ledger_transactions',
                            'reversal_reason_code',
                            'INSERT'
                        ),
                        (
                            'public.ledger_transactions',
                            'reversal_note',
                            'INSERT'
                        ),
                        (
                            'public.ledger_transactions',
                            'created_at',
                            'INSERT'
                        ),
                        ('public.outbox_events', 'event_type', 'INSERT'),
                        ('public.outbox_events', 'aggregate_type', 'INSERT'),
                        ('public.outbox_events', 'aggregate_id', 'INSERT'),
                        ('public.outbox_events', 'request_id', 'INSERT'),
                        ('public.outbox_events', 'payload', 'INSERT'),
                        ('public.outbox_events', 'created_at', 'INSERT'),
                        ('public.accounts', 'currency', 'UPDATE'),
                        ('public.ledger_transactions', 'id', 'UPDATE'),
                        ('public.reconciliation_runs', 'status', 'UPDATE'),
                        ('public.reconciliation_runs', 'summary', 'UPDATE'),
                        (
                            'public.reconciliation_runs',
                            'completed_at',
                            'UPDATE'
                        ),
                        ('public.reconciliation_items', 'result', 'UPDATE'),
                        (
                            'public.reconciliation_items',
                            'mismatch_code',
                            'UPDATE'
                        ),
                        (
                            'public.reconciliation_items',
                            'matched_transaction_id',
                            'UPDATE'
                        ),
                        (
                            'public.reconciliation_items',
                            'resolution_status',
                            'UPDATE'
                        ),
                        (
                            'public.reconciliation_items',
                            'resolution_note',
                            'UPDATE'
                        ),
                        (
                            'public.reconciliation_items',
                            'resolved_at',
                            'UPDATE'
                        )
                ),
                required_triggers(relation_name, trigger_name) AS (
                    VALUES
                        (
                            'public.accounts',
                            'account_identity_is_immutable'
                        ),
                        (
                            'public.ledger_transactions',
                            'ledger_transactions_are_immutable'
                        ),
                        (
                            'public.ledger_transactions',
                            'ledger_transaction_must_balance'
                        ),
                        (
                            'public.ledger_transactions',
                            'ledger_transactions_assign_posting_sequence'
                        ),
                        (
                            'public.ledger_entries',
                            'ledger_entries_are_immutable'
                        ),
                        (
                            'public.ledger_entries',
                            'ledger_entry_must_balance'
                        ),
                        (
                            'public.outbox_events',
                            'outbox_events_assign_commit_order_id'
                        ),
                        (
                            'public.outbox_events',
                            'outbox_events_are_immutable'
                        ),
                        (
                            'public.reconciliation_runs',
                            'reconciliation_runs_guard_updates'
                        ),
                        (
                            'public.reconciliation_runs',
                            'reconciliation_runs_are_consistent'
                        ),
                        (
                            'public.reconciliation_items',
                            'reconciliation_items_guard_updates'
                        ),
                        (
                            'public.reconciliation_items',
                            'reconciliation_items_are_consistent'
                        )
                ),
                required_sequences(schema_name, sequence_name) AS (
                    VALUES
                        (
                            'public',
                            'ledger_transactions_posting_sequence_seq'
                        ),
                        ('public', 'outbox_events_id_seq')
                )
                SELECT
                    has_function_privilege(
                        current_user,
                        'public.ledger_assert_balanced(uuid)',
                        'EXECUTE'
                        )
                        AND has_function_privilege(
                            current_user,
                            'public.reconciliation_insert_ledger_only(uuid,uuid,uuid)',
                            'EXECUTE'
                        )
                        AND has_table_privilege(
                            current_user,
                            'public.accounts',
                            'INSERT'
                        )
                        AND has_table_privilege(
                            current_user,
                            'public.ledger_entries',
                            'INSERT'
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM required_column_privileges AS required
                            WHERE NOT has_column_privilege(
                                current_user,
                                required.relation_name,
                                required.column_name,
                                required.privilege_name
                            )
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM required_triggers AS required
                            WHERE NOT EXISTS (
                                SELECT 1
                                FROM pg_catalog.pg_trigger AS installed
                                WHERE installed.tgrelid =
                                    required.relation_name::regclass
                                    AND installed.tgname = required.trigger_name
                                    AND installed.tgenabled IN ('O', 'A')
                            )
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM required_sequences AS required
                            WHERE NOT EXISTS (
                                SELECT 1
                                FROM pg_catalog.pg_class AS installed
                                JOIN pg_catalog.pg_namespace AS namespace
                                    ON namespace.oid = installed.relnamespace
                                WHERE namespace.nspname = required.schema_name
                                    AND installed.relname =
                                        required.sequence_name
                                    AND installed.relkind = 'S'
                            )
                        )
                        AND NOT EXISTS (SELECT 1 FROM required_schema)
                """
            )
        )
        if ready is not True:
            raise LedgerError(
                503,
                "database unavailable",
                code="database_unavailable",
            )
    except SQLAlchemyError as exc:
        raise LedgerError(
            503,
            "database unavailable",
            code="database_unavailable",
        ) from exc
    return {"status": "ok"}


@app.get(
    "/livez",
    response_model=HealthResponse,
    tags=["operations"],
    summary="Check process liveness",
)
async def livez() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/readyz",
    response_model=HealthResponse,
    tags=["operations"],
    summary="Check database readiness",
    responses={503: problem_response_spec("Database unavailable.", retryable=True)},
)
def readyz(session: Annotated[Session, Depends(get_session)]) -> dict[str, str]:
    return _database_readiness(session)


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["operations"],
    summary="Compatibility alias for readiness",
    responses={503: problem_response_spec("Database unavailable.", retryable=True)},
)
def health(session: Annotated[Session, Depends(get_session)]) -> dict[str, str]:
    return _database_readiness(session)


@app.get(
    "/metrics",
    tags=["operations"],
    summary="Read bounded Prometheus metrics",
    include_in_schema=False,
)
def metrics() -> Response:
    return metrics_response()


# These routes preserve the original exercise API for existing clients. The
# console and OpenAPI advertise only /api/v1, preventing two competing contracts.
LegacyIdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^[!-~]+$",
    ),
]


@app.post(
    "/accounts",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_account(
    body: AccountCreate,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    result = services.create_account(session, body.currency)
    return {
        key: value for key, value in result.payload.items() if key != "display_name"
    }


@app.post(
    "/accounts/{account_id}/deposits",
    response_model=DepositResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def deposit(
    account_id: UUID,
    body: DepositCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: LegacyIdempotencyKey,
) -> dict[str, object]:
    result = services.deposit(
        session,
        account_id,
        body.amount,
        idempotency_key=idempotency_key,
        request_id=getattr(request.state, "request_id", None),
    )
    response.headers["Idempotent-Replayed"] = str(result.replayed).lower()
    return result.payload


@app.post(
    "/transfers",
    response_model=TransferResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def transfer(
    body: TransferCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: LegacyIdempotencyKey,
) -> dict[str, object]:
    result = services.transfer(
        session,
        body.source_account_id,
        body.destination_account_id,
        body.amount,
        idempotency_key,
        request_id=getattr(request.state, "request_id", None),
    )
    response.headers["Idempotent-Replayed"] = str(result.replayed).lower()
    return result.payload


@app.get(
    "/accounts/{account_id}/statement",
    response_model=StatementResponse,
    include_in_schema=False,
)
def statement(
    account_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object]:
    return services.get_statement(session, account_id)


@app.api_route(
    "/api/{unmatched_path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
def unmatched_api(request: Request, unmatched_path: str) -> None:
    allowed_methods: set[str] = set()
    for product_router in api_routers:
        for route in product_router.routes:
            match, _ = route.matches(request.scope)
            if match is Match.PARTIAL:
                allowed_methods.update(getattr(route, "methods", set()) or set())
    if allowed_methods:
        raise HTTPException(
            status_code=405,
            headers={"Allow": ", ".join(sorted(allowed_methods))},
        )
    del unmatched_path
    raise LedgerError(404, "API route not found", code="not_found")


frontend_dist = next(
    (candidate for candidate in FRONTEND_DIST_CANDIDATES if candidate.is_dir()),
    None,
)
if frontend_dist is not None:
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="console")
else:

    @app.get("/", include_in_schema=False)
    def console_not_built() -> HTMLResponse:
        return HTMLResponse(
            "<h1>LedgerLite</h1><p>Build the console with "
            "<code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>.</p>"
        )


__all__ = [
    "app",
    "health",
    "livez",
    "readyz",
    "redoc_docs",
    "swagger_docs",
    "swagger_ui_redirect",
]
