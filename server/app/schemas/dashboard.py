from datetime import date

from pydantic import BaseModel, Field

from app.schemas.manual_bill import ManualBillDue


class DueCard(BaseModel):
    statement_id: str
    card_id: str | None = None
    bank: str
    display_name: str | None = None
    last4: str | None = None
    remaining_minor: int
    due_date: date
    days_left: int
    status: str
    currency: str
    parse_status: str


class DueResponse(BaseModel):
    # None when multiple currencies are outstanding: cross-currency arithmetic
    # is prohibited; use totals_by_currency instead.
    total_remaining_minor: int | None
    currency: str
    totals_by_currency: dict[str, int] = Field(default_factory=dict)
    cards: list[DueCard]


class ManualDueResponse(BaseModel):
    total_remaining_minor: int
    items: list[ManualBillDue]


class CreditLimitBank(BaseModel):
    bank_name: str
    currency: str
    limit_minor: int


class DashboardResponse(BaseModel):
    due: DueResponse
    manual_due: ManualDueResponse
    total_credit_limit_minor: int
    credit_limit_banks: list[CreditLimitBank]
    known_utilization_percent: float | None
    next_due_date: date | None
