import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ParsedBatch(TimestampMixin, Base):
    __tablename__ = "parsed_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String(16))
    source_locator: Mapped[str | None] = mapped_column(String(512), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    provider_id: Mapped[str] = mapped_column(String(64), index=True)
    original_format: Mapped[str] = mapped_column(String(16))
    transaction_count: Mapped[int] = mapped_column(Integer())
    entries: Mapped[list["ParsedEntry"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class ParsedEntry(TimestampMixin, Base):
    __tablename__ = "parsed_entries"
    __table_args__ = (UniqueConstraint("batch_id", "source_line", name="uq_parsed_entry_batch_line"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id: Mapped[str] = mapped_column(ForeignKey("parsed_batches.id", ondelete="CASCADE"), index=True)
    source_line: Mapped[int] = mapped_column(Integer())
    transaction_date: Mapped[date] = mapped_column(Date())
    posting_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    amount_minor: Mapped[int] = mapped_column(Integer())
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str] = mapped_column(Text())
    card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    batch: Mapped[ParsedBatch] = relationship(back_populates="entries")
