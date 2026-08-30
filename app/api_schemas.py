from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StringConstraints

from app.schemas import (
    APIModel,
    CurrencyCode,
    DepositCreate,
    DepositResponse,
    MoneyString,
    SignedMoneyString,
    TransferCreate,
    TransferResponse,
)

TransactionType = Literal["deposit", "transfer", "reversal"]
ReversalReasonCode = Literal[
    "duplicate",
    "customer_request",
    "operator_correction",
    "other",
]
ReconciliationRunStatus = Literal["pending", "completed"]
ReconciliationResult = Literal[
    "pending",
    "matched",
    "provider_only",
    "ledger_only",
    "mismatched",
    "duplicate",
]
ReconciliationMismatchCode = Literal[
    "transaction_not_found",
    "amount_mismatch",
    "currency_mismatch",
    "transaction_type_mismatch",
    "outside_period",
    "duplicate_claim",
    "unclaimed_ledger_transaction",
]
ReconciliationResolutionStatus = Literal["open", "matched", "ignored"]
Count = Annotated[int, Field(strict=True, ge=0)]
Cursor = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]
ReversalNote = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=240),
]
ProblemType = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
ProblemTitle = Annotated[str, StringConstraints(min_length=1, max_length=120)]
ProblemCode = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$"),
]
ProblemDetail = Annotated[str, StringConstraints(min_length=1, max_length=500)]
ProblemInstance = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
RequestId = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class ProblemResponse(APIModel):
    type: ProblemType
    title: ProblemTitle
    status: Annotated[int, Field(strict=True, ge=400, le=599)]
    detail: ProblemDetail
    code: ProblemCode
    instance: ProblemInstance
    request_id: RequestId


def problem_response_spec(
    description: str, *, retryable: bool = False
) -> dict[str, object]:
    """Describe the exact RFC 9457 media type returned by global handlers."""

    headers: dict[str, object] = {
        "X-Request-ID": {
            "description": "Correlation ID echoed in the problem document.",
            "schema": {"type": "string"},
        }
    }
    if retryable:
        headers["Retry-After"] = {
            "description": "Minimum retry delay in seconds.",
            "schema": {"type": "integer", "minimum": 1},
        }
    return {
        "description": description,
        "headers": headers,
        "content": {
            "application/problem+json": {
                "schema": ProblemResponse.model_json_schema(),
            }
        },
    }


class AccountCreate(APIModel):
    currency: CurrencyCode
    display_name: DisplayName | None = None


class ConsoleCapabilities(APIModel):
    documentation: StrictBool


class OverviewCurrency(APIModel):
    currency: CurrencyCode
    customer_accounts: Count
    total_customer_funds: MoneyString
    clearing_balance: SignedMoneyString
    net_imbalance: SignedMoneyString


class OverviewIntegrity(APIModel):
    transaction_count: Count
    entry_count: Count
    reversal_count: Count
    unbalanced_transaction_count: Count
    replay_count: Count
    open_reconciliation_exceptions: Count


class OverviewResponse(APIModel):
    as_of: datetime
    currencies: list[OverviewCurrency]
    integrity: OverviewIntegrity


class AccountItem(APIModel):
    id: UUID
    display_name: DisplayName | None
    currency: CurrencyCode
    balance: SignedMoneyString
    created_at: datetime


class AccountResponse(AccountItem):
    """Created account using the same shape as account read models."""


class AccountsPage(APIModel):
    items: list[AccountItem]
    next_cursor: Cursor | None


class StatementItem(APIModel):
    id: UUID
    transaction_id: UUID
    type: TransactionType
    amount: SignedMoneyString
    currency: CurrencyCode
    counterparty_account_id: UUID | None
    created_at: datetime
    balance_after: SignedMoneyString


class StatementPage(APIModel):
    account: AccountItem
    balance: SignedMoneyString
    items: list[StatementItem]
    next_cursor: Cursor | None


class TransactionItem(APIModel):
    id: UUID
    type: TransactionType
    amount: MoneyString
    currency: CurrencyCode
    source_account_id: UUID
    destination_account_id: UUID
    source_display_name: DisplayName | None
    destination_display_name: DisplayName | None
    created_at: datetime
    reverses_transaction_id: UUID | None
    reversed_by_transaction_id: UUID | None
    reversal_reason_code: ReversalReasonCode | None
    reversal_note: ReversalNote | None


class TransactionsPage(APIModel):
    items: list[TransactionItem]
    next_cursor: Cursor | None


class PostingItem(APIModel):
    id: UUID
    sequence: Literal[1, 2]
    account_id: UUID
    account_display_name: DisplayName | None
    amount: SignedMoneyString
    currency: CurrencyCode
    created_at: datetime


class TransactionIntegrity(APIModel):
    entry_count: Count
    posting_sum: SignedMoneyString
    balanced: StrictBool
    currency_consistent: StrictBool


class TransactionDetail(TransactionItem):
    entries: list[PostingItem]
    integrity: TransactionIntegrity


class ReversalCreate(APIModel):
    reason_code: ReversalReasonCode
    note: ReversalNote | None = None


class ReversalResponse(APIModel):
    transaction_id: UUID
    reverses_transaction_id: UUID
    source_account_id: UUID
    destination_account_id: UUID
    amount: MoneyString
    currency: CurrencyCode
    reason_code: ReversalReasonCode
    note: ReversalNote | None
    created_at: datetime


class ReconciliationCounts(APIModel):
    matched: Count
    provider_only: Count
    ledger_only: Count
    mismatched: Count
    duplicate: Count
    open_exceptions: Count


class ReconciliationGrossVolume(APIModel):
    currency: CurrencyCode
    provider_total: SignedMoneyString
    ledger_total: SignedMoneyString
    difference: SignedMoneyString


class ReconciliationSummary(APIModel):
    counts: ReconciliationCounts
    gross_volume: ReconciliationGrossVolume


class ReconciliationRunResponse(APIModel):
    id: UUID
    provider: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    currency: CurrencyCode
    period_start: datetime
    period_end: datetime
    status: ReconciliationRunStatus
    summary: ReconciliationSummary | None
    created_at: datetime
    completed_at: datetime | None


class ReconciliationRunsResponse(APIModel):
    items: list[ReconciliationRunResponse]


class ReconciliationItemResponse(APIModel):
    id: UUID
    run_id: UUID
    provider_reference: (
        Annotated[
            str,
            StringConstraints(min_length=1, max_length=80),
        ]
        | None
    )
    claimed_transaction_id: UUID | None
    matched_transaction_id: UUID | None
    amount: MoneyString
    currency: CurrencyCode
    occurred_at: datetime
    result: ReconciliationResult
    mismatch_code: ReconciliationMismatchCode | None
    resolution_status: ReconciliationResolutionStatus
    resolution_note: ReversalNote | None
    created_at: datetime
    resolved_at: datetime | None


class ReconciliationItemsPage(APIModel):
    items: list[ReconciliationItemResponse]
    next_cursor: Cursor | None


class ReconciliationMatchRequest(APIModel):
    transaction_id: UUID
    note: ReversalNote | None = None


class ReconciliationIgnoreRequest(APIModel):
    reason: ReversalNote


__all__ = [
    "AccountCreate",
    "AccountItem",
    "AccountResponse",
    "AccountsPage",
    "ConsoleCapabilities",
    "DepositCreate",
    "DepositResponse",
    "OverviewCurrency",
    "OverviewIntegrity",
    "OverviewResponse",
    "PostingItem",
    "ProblemResponse",
    "problem_response_spec",
    "ReconciliationCounts",
    "ReconciliationGrossVolume",
    "ReconciliationIgnoreRequest",
    "ReconciliationItemResponse",
    "ReconciliationItemsPage",
    "ReconciliationMismatchCode",
    "ReconciliationMatchRequest",
    "ReconciliationResolutionStatus",
    "ReconciliationResult",
    "ReconciliationRunResponse",
    "ReconciliationRunsResponse",
    "ReconciliationRunStatus",
    "ReconciliationSummary",
    "ReversalCreate",
    "ReversalReasonCode",
    "ReversalResponse",
    "StatementItem",
    "StatementPage",
    "TransactionDetail",
    "TransactionIntegrity",
    "TransactionItem",
    "TransactionType",
    "TransactionsPage",
    "TransferCreate",
    "TransferResponse",
]
