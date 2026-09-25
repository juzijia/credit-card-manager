from datetime import date, datetime
from pydantic import BaseModel, ConfigDict, Field


class MortgagePlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    loan_type: str = Field(pattern="^(commercial|provident_fund|combined)$")
    repayment_method: str = Field(pattern="^(equal_payment|equal_principal)$")
    loan_term_months: int = Field(gt=0, le=480)

    commercial_principal_minor: int = Field(ge=0, default=0)
    commercial_rate_mode: str | None = None
    lpr_rate: float | None = None
    bp: float | None = None
    commercial_actual_rate: float | None = None

    fund_principal_minor: int = Field(ge=0, default=0)
    fund_rate: float | None = None

    first_payment_date: date
    start_period: int = Field(default=1, ge=1, le=480)
    reminder_enabled: bool = True


class MortgagePlanRead(MortgagePlanCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    enabled: bool
    manual_bill_id: str | None = None
    created_at: datetime
    updated_at: datetime


class MortgageSaveResponse(BaseModel):
    plan: MortgagePlanRead
    manual_bill_id: str

