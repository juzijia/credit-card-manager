from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.optional.interest_free import (
    EstimationLevel,
    StatementBoundaryState,
)


class HistoricalInterestFreeEstimateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    card_id: str
    spend_date: date
    statement_date: date | None = None
    due_date: date
    interest_free_days: int
    posting_date: date | None = None
    estimation_level: EstimationLevel = EstimationLevel.ACTUAL


class FutureInterestFreeEstimateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    card_id: str
    spend_date: date
    selected_statement_date: date
    selected_due_date: date
    conservative_interest_free_days: int
    theoretical_max_interest_free_days: int | None = None
    boundary_state: StatementBoundaryState
    conservative: bool = True
    estimation_level: EstimationLevel = EstimationLevel.FULLY_ESTIMATED
    rule_source: Literal[
        "explicit_card", "bank_billing_rule", "card_history", "product_override"
    ] = "explicit_card"
    resolved_rule_type: Literal["fixed_day", "days_after_statement"] | None = None
    resolved_rule_value: int | None = None
    history_evidence_count: int | None = None
    last_verified_date: date | None = None


InterestFreeEstimateRead = HistoricalInterestFreeEstimateRead | FutureInterestFreeEstimateRead


class InterestFreeRead(BaseModel):
    available: bool
    mode: Literal["historical_actual", "future_estimate"]
    reason: Literal[
        "missing_transaction_date",
        "missing_statement_facts",
        "due_date_before_transaction_date",
        "missing_statement_day",
        "invalid_statement_day",
        "invalid_due_date_rule",
        "unresolved_due_date_rule",
        "ambiguous_due_date_rule",
        "conflicting_due_date_rule",
    ] | None = Field(default=None, description="Stable unavailability code; null when available.")
    estimate: HistoricalInterestFreeEstimateRead | FutureInterestFreeEstimateRead | None = None
