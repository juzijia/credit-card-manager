from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RepaymentChannel = Literal["bank_app", "unionpay", "alipay", "wechat", "other"]


class RepaymentCreate(BaseModel):
    amount_minor: int = Field(gt=0)
    paid_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)
    repayment_channel: RepaymentChannel | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("paid_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("paid_at must include a timezone offset")
        return value


class MarkPaidCreate(BaseModel):
    repayment_channel: RepaymentChannel | None = None


class RepaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    statement_id: str
    amount_minor: int
    paid_at: datetime
    note: str | None
    repayment_channel: RepaymentChannel | None
    idempotency_key: str
    created_at: datetime

    @field_validator("paid_at", "created_at", mode="before")
    @classmethod
    def attach_utc_to_stored_timestamps(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
