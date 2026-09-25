import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, utc_now

if TYPE_CHECKING:
    from app.models.statement import Statement


class Repayment(TimestampMixin, Base):
    __tablename__ = "repayments"
    __table_args__ = (
        UniqueConstraint(
            "statement_id", "idempotency_key", name="uq_repayment_idempotency"
        ),
        CheckConstraint("amount_minor > 0", name="ck_repayment_amount_positive"),
        CheckConstraint(
            "repayment_channel IS NULL OR repayment_channel IN "
            "('bank_app', 'unionpay', 'alipay', 'wechat', 'other')",
            name="ck_repayment_channel_valid",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    statement_id: Mapped[str] = mapped_column(
        ForeignKey("statements.id", ondelete="CASCADE"), index=True
    )
    amount_minor: Mapped[int] = mapped_column(Integer())
    paid_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    repayment_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))

    statement: Mapped["Statement"] = relationship(back_populates="repayments")
