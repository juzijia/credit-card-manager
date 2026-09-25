import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.statement import Statement


class Card(TimestampMixin, Base):
    __tablename__ = "cards"
    __table_args__ = (
        Index(
            "uq_cards_bank_last4_currency",
            "bank_code",
            "last4",
            "currency",
            unique=True,
        ),
        CheckConstraint("length(last4) = 4", name="ck_cards_last4_length"),
        CheckConstraint(
            "statement_day IS NULL OR statement_day BETWEEN 1 AND 31",
            name="ck_cards_statement_day",
        ),
        CheckConstraint(
            "due_day IS NULL OR due_day BETWEEN 1 AND 31", name="ck_cards_due_day"
        ),
        CheckConstraint(
            "credit_limit_minor IS NULL OR credit_limit_minor >= 0",
            name="ck_cards_credit_limit_nonnegative",
        ),
        CheckConstraint(
            "due_date_rule_type IN ('days_after_statement', 'fixed_day', 'observed_only')",
            name="ck_cards_due_date_rule_type",
        ),
        CheckConstraint(
            "(due_date_rule_type = 'observed_only' AND due_date_rule_value IS NULL) OR "
            "(due_date_rule_type = 'days_after_statement' AND due_date_rule_value BETWEEN 1 AND 100) OR "
            "(due_date_rule_type = 'fixed_day' AND due_date_rule_value BETWEEN 1 AND 31)",
            name="ck_cards_due_date_rule_value",
        ),
        CheckConstraint(
            "statement_day_boundary IN ('current_cycle', 'next_cycle', 'uncertain')",
            name="ck_cards_statement_day_boundary",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    bank_code: Mapped[str] = mapped_column(String(16), index=True)
    bank_name: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128))
    last4: Mapped[str] = mapped_column(String(4), index=True)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    credit_limit_minor: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    statement_day: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    due_day: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    due_date_rule_type: Mapped[str] = mapped_column(
        String(32), default="observed_only"
    )
    due_date_rule_value: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    statement_day_boundary: Mapped[str] = mapped_column(
        String(32), default="uncertain", server_default="uncertain"
    )
    hidden: Mapped[bool] = mapped_column(Boolean(), default=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True)
    reminder_enabled: Mapped[bool] = mapped_column(Boolean(), default=True)
    card_product: Mapped[str | None] = mapped_column(String(64), nullable=True)
    annual_fee_minor: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    annual_fee_policy: Mapped[str | None] = mapped_column(String(256), nullable=True)
    annual_fee_charge_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    annual_fee_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active", index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True, index=True)

    statements: Mapped[list["Statement"]] = relationship(
        back_populates="card", cascade="all, delete-orphan"
    )
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="card"
    )

    @property
    def is_hidden(self) -> bool:
        return bool(self.hidden)

    @is_hidden.setter
    def is_hidden(self, value: bool) -> None:
        self.hidden = bool(value)

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.deleted_at is None

    @property
    def is_closed(self) -> bool:
        return self.status == "closed"

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
