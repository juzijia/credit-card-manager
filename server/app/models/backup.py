from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class BackupSetting(TimestampMixin, Base):
    __tablename__ = "backup_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    webdav_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    webdav_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    schedule_type: Mapped[str] = mapped_column(String(16), default="off")
    cron_expr: Mapped[str] = mapped_column(String(128), default="0 3 * * *")
    retention_count: Mapped[int] = mapped_column(Integer(), default=14)
    webdav_verified_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    last_local_backup_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    last_local_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_remote_backup_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    last_remote_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
