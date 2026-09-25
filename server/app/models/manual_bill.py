import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ManualBill(TimestampMixin, Base):
    __tablename__ = "manual_bills"
    __table_args__ = (
        CheckConstraint("statement_day BETWEEN 1 AND 31", name="ck_manual_bills_statement_day"),
        CheckConstraint("due_day BETWEEN 1 AND 31", name="ck_manual_bills_due_day"),
        CheckConstraint("monthly_amount_minor > 0", name="ck_manual_bills_amount_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    bill_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    statement_day: Mapped[int] = mapped_column(Integer())
    due_day: Mapped[int] = mapped_column(Integer())
    monthly_amount_minor: Mapped[int] = mapped_column(Integer())
    reminder_enabled: Mapped[bool] = mapped_column(Boolean(), default=True)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True)
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class ManualBillPayment(TimestampMixin, Base):
    __tablename__ = "manual_bill_payments"
    __table_args__ = (
        UniqueConstraint("manual_bill_id", "period", name="uq_manual_bill_payment_period"),
        CheckConstraint(
            "repayment_channel IS NULL OR repayment_channel IN "
            "('bank_app', 'unionpay', 'alipay', 'wechat', 'other')",
            name="ck_manual_bill_payment_channel_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    manual_bill_id: Mapped[str] = mapped_column(ForeignKey("manual_bills.id", ondelete="CASCADE"), index=True)
    period: Mapped[str] = mapped_column(String(7))
    due_date: Mapped[date] = mapped_column(Date())
    amount_minor: Mapped[int] = mapped_column(Integer())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    repayment_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ManualBillNotificationLog(Base):
    __tablename__ = "manual_bill_notification_logs"
    __table_args__ = (
        UniqueConstraint("manual_bill_payment_id", "offset_days", "provider", name="uq_manual_bill_notification_offset_provider"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    manual_bill_payment_id: Mapped[str] = mapped_column(ForeignKey("manual_bill_payments.id", ondelete="CASCADE"), index=True)
    offset_days: Mapped[int] = mapped_column(Integer(), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    sent_at: Mapped[datetime] = mapped_column(DateTime(), default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(16), default="success")
    response_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
