from datetime import date
from typing import Literal

from pydantic import BaseModel


class AnalyticsPeriod(BaseModel):
    months: int
    start_month: str
    end_month: str
    currency: str
    generated_on: date
    transaction_date_basis: str = "transaction_date_then_posting_date"


class AnalyticsCoverage(BaseModel):
    status: Literal["complete", "partial", "missing", "unknown"]
    complete_statement_count: int = 0
    partial_statement_count: int = 0
    unknown_transaction_count: int = 0
    undated_transaction_count: int = 0
    latest_statement_date: date | None = None


class BankUtilization(BaseModel):
    bank_code: str
    bank_name: str
    outstanding_minor: int
    credit_limit_minor: int | None
    outstanding_to_known_limit_percent: float | None = None
    # Compatibility alias. New consumers should use the explicit field above.
    utilization_percent: float | None


class CardUtilization(BaseModel):
    card_id: str
    bank_code: str
    bank_name: str
    display_name: str
    last4: str
    outstanding_minor: int
    credit_limit_minor: int | None
    outstanding_to_known_limit_percent: float | None = None
    # Compatibility alias. New consumers should use the explicit field above.
    utilization_percent: float | None


class MonthlyBankUsage(BaseModel):
    bank_code: str
    bank_name: str
    amount_minor: int
    credit_limit_minor: int | None
    monthly_spend_to_limit_percent: float | None = None
    # Compatibility alias. This is not real-time credit utilization.
    utilization_percent: float | None


class MonthlyConsumption(BaseModel):
    month: str
    amount_minor: int
    transaction_count: int
    gross_purchase_minor: int = 0
    refund_minor: int = 0
    net_consumption_minor: int = 0
    bank_usage: list[MonthlyBankUsage]
    credit_card_amount_minor: int = 0
    manual_bill_amount_minor: int = 0
    manual_bill_count: int = 0
    total_amount_minor: int = 0


class MonthlyDue(BaseModel):
    month: str
    credit_card_remaining_minor: int
    manual_bill_remaining_minor: int
    total_remaining_minor: int


class MonthlyRepayment(BaseModel):
    month: str
    credit_card_paid_minor: int
    manual_bill_paid_minor: int
    total_paid_minor: int


class AnalyticsResponse(BaseModel):
    period: AnalyticsPeriod
    coverage: AnalyticsCoverage
    monthly_consumption: list[MonthlyConsumption]
    monthly_due: list[MonthlyDue]
    bank_utilization: list[BankUtilization]
    card_utilization: list[CardUtilization]
    repayment_trend: list[MonthlyRepayment]
