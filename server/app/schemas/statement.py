from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.statement import ParseStatus
from app.schemas.card import validate_currency_code


class StatementCreate(BaseModel):
    card_id: str
    bank_code: str | None = None
    statement_date: date
    due_date: date
    total_due_minor: int = Field(ge=0)
    minimum_due_minor: int | None = Field(default=None, ge=0)
    currency: str = Field(default="CNY")
    parse_status: ParseStatus = ParseStatus.SUMMARY_OK

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return validate_currency_code(value)

    @model_validator(mode="after")
    def validate_dates_and_amounts(self) -> "StatementCreate":
        if self.due_date < self.statement_date:
            raise ValueError("due_date must be on or after statement_date")
        if (
            self.minimum_due_minor is not None
            and self.minimum_due_minor > self.total_due_minor
        ):
            raise ValueError("minimum_due_minor cannot exceed total_due_minor")
        return self


class StatementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    bank_code: str
    card_id: str | None = None
    source_import_id: str | None
    statement_date: date
    due_date: date
    total_due_minor: int
    minimum_due_minor: int | None
    currency: str
    parse_status: ParseStatus
    paid_minor: int
    remaining_minor: int
    status: str
    created_at: datetime
    updated_at: datetime
    card_bank_name: str | None = None
    card_display_name: str | None = None
    card_last4: str | None = None
    card_product: str | None = None
    card_status: str | None = None
    card_is_deleted: bool = False


class StatementAnalysisMetricsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    gross_purchase_minor: int = 0
    purchase_count: int = 0
    refund_observed_minor: int = 0
    period_net_consumption_minor: int = 0
    observed_repayment_activity_minor: int = 0
    annual_fee_activity_minor: int = 0
    fee_activity_minor: int = 0
    interest_activity_minor: int = 0
    reward_activity_minor: int = 0
    cash_advance_activity_minor: int = 0
    reversal_activity_minor: int = 0
    adjustment_activity_minor: int = 0
    unknown_activity_minor: int = 0


class StatementAnalysisCoverageRead(BaseModel):
    status: Literal["complete", "partial", "missing", "unknown"]
    unknown_transaction_count: int = 0
    undated_transaction_count: int = 0


class StatementAnalysisRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    statement_id: str
    metrics: StatementAnalysisMetricsRead
    coverage: StatementAnalysisCoverageRead
