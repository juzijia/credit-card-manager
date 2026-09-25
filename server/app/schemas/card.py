from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DueDateRuleType = Literal["days_after_statement", "fixed_day", "observed_only"]
StatementDayBoundary = Literal["current_cycle", "next_cycle", "uncertain"]


def validate_currency_code(value: str) -> str:
    """3-letter alphabetic currency-code format; currency safety, not an FX enablement."""
    normalized = value.strip().upper()
    if len(normalized) == 3 and normalized.isalpha():
        return normalized
    raise ValueError("currency must be a 3-letter alphabetic currency code")


def _validate_due_date_rule(rule_type: str, value: int | None) -> None:
    if rule_type == "observed_only":
        if value is not None:
            raise ValueError("observed_only requires a null due_date_rule_value")
    elif rule_type == "fixed_day":
        if value is None or not 1 <= value <= 31:
            raise ValueError("fixed_day requires due_date_rule_value between 1 and 31")
    elif value is None or not 1 <= value <= 100:
        raise ValueError(
            "days_after_statement requires due_date_rule_value between 1 and 100"
        )


class CardBase(BaseModel):
    bank_code: str = Field(min_length=2, max_length=16)
    bank_name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    last4: str = Field(pattern=r"^\d{4}$")
    currency: str = Field(default="CNY")
    credit_limit_minor: int | None = Field(default=None, ge=0)
    statement_day: int | None = Field(default=None, ge=1, le=31)
    due_day: int | None = Field(default=None, ge=1, le=31)
    due_date_rule_type: DueDateRuleType = "observed_only"
    due_date_rule_value: int | None = None
    statement_day_boundary: StatementDayBoundary = "uncertain"
    hidden: bool = False
    enabled: bool = True
    reminder_enabled: bool = True
    card_product: str | None = Field(default=None, max_length=64)
    annual_fee_minor: int | None = Field(default=None, ge=0)
    annual_fee_policy: str | None = Field(default=None, max_length=256)
    annual_fee_charge_date: str | None = Field(default=None, max_length=32)
    annual_fee_source: str | None = Field(default=None, max_length=16)
    status: Literal["active", "closed"] = "active"
    closed_at: datetime | None = None
    deleted_at: datetime | None = None
    is_hidden: bool = False

    @field_validator("bank_code")
    @classmethod
    def uppercase_code(cls, value: str) -> str:
        return value.upper()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return validate_currency_code(value)

    @model_validator(mode="after")
    def validate_due_date_rule(self) -> "CardBase":
        _validate_due_date_rule(self.due_date_rule_type, self.due_date_rule_value)
        return self


class CardCreate(CardBase):
    pass


class CardUpdate(BaseModel):
    bank_name: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    credit_limit_minor: int | None = Field(default=None, ge=0)
    statement_day: int | None = Field(default=None, ge=1, le=31)
    due_day: int | None = Field(default=None, ge=1, le=31)
    due_date_rule_type: DueDateRuleType | None = None
    due_date_rule_value: int | None = None
    statement_day_boundary: StatementDayBoundary | None = None
    hidden: bool | None = None
    enabled: bool | None = None
    reminder_enabled: bool | None = None
    card_product: str | None = Field(default=None, max_length=64)
    annual_fee_minor: int | None = Field(default=None, ge=0)
    annual_fee_policy: str | None = Field(default=None, max_length=256)
    annual_fee_charge_date: str | None = Field(default=None, max_length=32)
    annual_fee_source: str | None = Field(default=None, max_length=16)
    status: Literal["active", "closed"] | None = None
    closed_at: datetime | None = None
    deleted_at: datetime | None = None
    is_hidden: bool | None = None

    @model_validator(mode="after")
    def validate_due_date_rule_update(self) -> "CardUpdate":
        fields = self.model_fields_set
        if "statement_day_boundary" in fields and self.statement_day_boundary is None:
            raise ValueError("statement_day_boundary cannot be null")
        if "due_date_rule_type" in fields or "due_date_rule_value" in fields:
            if "due_date_rule_type" not in fields or "due_date_rule_value" not in fields:
                raise ValueError(
                    "due_date_rule_type and due_date_rule_value must be updated together"
                )
            if self.due_date_rule_type is None:
                raise ValueError("due_date_rule_type cannot be null")
            _validate_due_date_rule(
                self.due_date_rule_type, self.due_date_rule_value
            )
        return self


class CardRead(CardBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime


class CardDeletionPreview(BaseModel):
    card_id: str
    bank_name: str
    display_name: str
    last4: str
    statement_count: int
    transaction_count: int
    repayment_count: int
    notification_setting_count: int
