from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.repayment import RepaymentChannel


class ManualBillCreate(BaseModel):
    bill_type: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    statement_day: int = Field(ge=1, le=31)
    due_day: int = Field(ge=1, le=31)
    monthly_amount_minor: int = Field(gt=0)
    reminder_enabled: bool = True


class ManualBillRead(ManualBillCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    enabled: bool
    source_type: str | None = None
    source_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ManualBillUpdate(BaseModel):
    bill_type: str | None = Field(None, min_length=1, max_length=32)
    name: str | None = Field(None, min_length=1, max_length=128)
    statement_day: int | None = Field(None, ge=1, le=31)
    due_day: int | None = Field(None, ge=1, le=31)
    monthly_amount_minor: int | None = Field(None, gt=0)
    reminder_enabled: bool | None = None
    enabled: bool | None = None


class ManualBillDue(BaseModel):
    payment_id: str
    bill_id: str
    bill_type: str
    name: str
    amount_minor: int
    due_date: date
    days_left: int
    reminder_enabled: bool


class ManualBillPaid(BaseModel):
    payment_id: str
    bill_id: str
    bill_type: str
    name: str
    amount_minor: int
    paid_at: datetime
    repayment_channel: RepaymentChannel | None
