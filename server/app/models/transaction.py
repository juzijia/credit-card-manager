import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.card import Card
    from app.models.statement import Statement


class Transaction(TimestampMixin, Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    statement_id: Mapped[str] = mapped_column(
        ForeignKey("statements.id", ondelete="CASCADE"), index=True
    )
    transaction_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    posting_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    amount_minor: Mapped[int] = mapped_column(Integer())
    currency: Mapped[str] = mapped_column(String(3))
    raw_description: Mapped[str] = mapped_column(Text())
    merchant: Mapped[str | None] = mapped_column(String(256), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    transaction_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    classification_source: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True
    )
    classification_rule_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    card_id: Mapped[str | None] = mapped_column(
        ForeignKey("cards.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    original_transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    statement: Mapped["Statement"] = relationship(back_populates="transactions")
    card: Mapped["Card | None"] = relationship(back_populates="transactions")
    original_transaction: Mapped["Transaction | None"] = relationship(
        remote_side=lambda: [Transaction.id],
        back_populates="refunds",
    )
    refunds: Mapped[list["Transaction"]] = relationship(
        back_populates="original_transaction",
    )
