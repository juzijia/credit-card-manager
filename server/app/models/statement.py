import uuid
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Date, Enum, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.card import Card
    from app.models.repayment import Repayment
    from app.models.transaction import Transaction


class ParseStatus(StrEnum):
    SUMMARY_OK = "summary_ok"
    TRANSACTIONS_PARTIAL = "transactions_partial"
    COMPLETE = "complete"
    FAILED = "failed"


class Statement(TimestampMixin, Base):
    __tablename__ = "statements"
    __table_args__ = (
        CheckConstraint("total_due_minor >= 0", name="ck_statement_total_nonnegative"),
        CheckConstraint(
            "minimum_due_minor IS NULL OR minimum_due_minor >= 0",
            name="ck_statement_minimum_nonnegative",
        ),
        Index(
            "uq_statement_period_card",
            "card_id",
            "statement_date",
            "currency",
            unique=True,
            sqlite_where=sa.text("card_id IS NOT NULL"),
        ),
        Index(
            "uq_statement_period_account",
            "bank_code",
            "statement_date",
            "currency",
            unique=True,
            sqlite_where=sa.text("card_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    bank_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    card_id: Mapped[str | None] = mapped_column(
        ForeignKey("cards.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    source_import_id: Mapped[str | None] = mapped_column(
        ForeignKey("import_records.id", ondelete="SET NULL"), nullable=True, index=True
    )
    statement_date: Mapped[date] = mapped_column(Date(), index=True)
    due_date: Mapped[date] = mapped_column(Date(), index=True)
    total_due_minor: Mapped[int] = mapped_column(Integer())
    minimum_due_minor: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    parse_status: Mapped[ParseStatus] = mapped_column(
        Enum(
            ParseStatus,
            native_enum=False,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        default=ParseStatus.SUMMARY_OK,
    )

    card: Mapped["Card | None"] = relationship(back_populates="statements")
    repayments: Mapped[list["Repayment"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan"
    )
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan"
    )
