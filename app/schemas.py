from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
)

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("999999999999999999.99")
MAX_MONEY_TEXT_LENGTH = len(str(MAX_AMOUNT))
MONEY_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$")


def parse_money(value: Any) -> Decimal:
    """Accept a plain decimal string and never route money through a float."""

    if not isinstance(value, str):
        raise ValueError("amount must be a decimal string")
    if len(value) > MAX_MONEY_TEXT_LENGTH:
        raise ValueError("amount exceeds NUMERIC(20, 2)")
    if not MONEY_PATTERN.fullmatch(value):
        raise ValueError("amount must be a decimal string with at most 2 decimals")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover - regex is stricter
        raise ValueError("amount is not a valid decimal") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be greater than zero")
    if amount > MAX_AMOUNT:
        raise ValueError("amount exceeds NUMERIC(20, 2)")
    return amount.quantize(CENT)


Money = Annotated[
    Decimal,
    BeforeValidator(parse_money),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$",
            "maxLength": MAX_MONEY_TEXT_LENGTH,
            "examples": ["25.00"],
            "description": "Positive decimal string with at most two decimals.",
        }
    ),
]
MoneyString = Annotated[
    str,
    StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)\.[0-9]{2}$"),
]
SignedMoneyString = Annotated[
    str,
    StringConstraints(pattern=r"^-?(?:0|[1-9][0-9]*)\.[0-9]{2}$"),
]
CurrencyCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=False,
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    ),
]


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorResponse(APIModel):
    detail: str = Field(examples=["account not found"])


class AccountCreate(APIModel):
    currency: CurrencyCode = Field(examples=["USD"])


class DepositCreate(APIModel):
    amount: Money


class TransferCreate(APIModel):
    source_account_id: UUID
    destination_account_id: UUID
    amount: Money


class AccountResponse(APIModel):
    id: UUID
    currency: CurrencyCode
    balance: SignedMoneyString
    created_at: datetime


class DepositResponse(APIModel):
    transaction_id: UUID
    account_id: UUID
    amount: MoneyString
    currency: CurrencyCode
    balance: SignedMoneyString
    created_at: datetime


class TransferResponse(APIModel):
    transaction_id: UUID
    source_account_id: UUID
    destination_account_id: UUID
    amount: MoneyString
    currency: CurrencyCode
    created_at: datetime


class StatementEntryResponse(APIModel):
    transaction_id: UUID
    type: Literal["deposit", "transfer", "reversal"]
    amount: SignedMoneyString
    currency: CurrencyCode
    created_at: datetime
    counterparty_account_id: UUID | None


class StatementResponse(APIModel):
    account_id: UUID
    currency: CurrencyCode
    balance: SignedMoneyString
    entries: list[StatementEntryResponse]


class HealthResponse(APIModel):
    status: Literal["ok"]
