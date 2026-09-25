import uuid
from datetime import date
from sqlalchemy import Boolean, Date, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin


class MortgagePlan(TimestampMixin, Base):
    __tablename__ = "mortgage_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128))
    loan_type: Mapped[str] = mapped_column(String(32))  # commercial, provident_fund, combined
    repayment_method: Mapped[str] = mapped_column(String(32))  # equal_payment, equal_principal
    loan_term_months: Mapped[int] = mapped_column(Integer())

    # Commercial parameters
    commercial_principal_minor: Mapped[int] = mapped_column(Integer(), default=0)
    commercial_rate_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)  # lpr_bp, actual
    lpr_rate: Mapped[float | None] = mapped_column(Float(), nullable=True)
    bp: Mapped[float | None] = mapped_column(Float(), nullable=True)
    commercial_actual_rate: Mapped[float | None] = mapped_column(Float(), nullable=True)

    # Provident fund parameters
    fund_principal_minor: Mapped[int] = mapped_column(Integer(), default=0)
    fund_rate: Mapped[float | None] = mapped_column(Float(), nullable=True)

    first_payment_date: Mapped[date] = mapped_column(Date())
    start_period: Mapped[int] = mapped_column(Integer(), default=1, server_default="1")
    reminder_enabled: Mapped[bool] = mapped_column(Boolean(), default=True)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True)

    # Optional relationship to ManualBill if synced
    manual_bill_id: Mapped[str | None] = mapped_column(
        ForeignKey("manual_bills.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

