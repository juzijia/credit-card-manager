import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ImapSyncRun(TimestampMixin, Base):
    __tablename__ = "imap_sync_runs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    trigger: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16))
    lookback_months: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    scanned_folders: Mapped[int] = mapped_column(Integer(), default=0)
    failed_folders: Mapped[int] = mapped_column(Integer(), default=0)
    scanned: Mapped[int] = mapped_column(Integer(), default=0)
    candidates: Mapped[int] = mapped_column(Integer(), default=0)
    complete_statements: Mapped[int] = mapped_column(Integer(), default=0)
    duplicate_statements: Mapped[int] = mapped_column(Integer(), default=0)
    parsed_batches: Mapped[int] = mapped_column(Integer(), default=0)
    duplicate_batches: Mapped[int] = mapped_column(Integer(), default=0)
    failed_candidates: Mapped[int] = mapped_column(Integer(), default=0)
    failure_reasons: Mapped[str] = mapped_column(Text(), default="[]")
