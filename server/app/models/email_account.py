import uuid

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class EmailAccount(TimestampMixin, Base):
    __tablename__ = "email_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(64))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer(), default=993)
    username: Mapped[str] = mapped_column(String(255))
    secret_ciphertext: Mapped[str] = mapped_column(Text())
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True)

    mailbox: Mapped[str] = mapped_column(String(128), default="INBOX")
    all_folders: Mapped[bool] = mapped_column(Boolean(), default=True)
    schedule_type: Mapped[str] = mapped_column(String(32), default="interval")
    poll_seconds: Mapped[int] = mapped_column(Integer(), default=1800)
    daily_time: Mapped[str] = mapped_column(String(16), default="08:00")
    cron_expr: Mapped[str] = mapped_column(String(64), default="0 8 * * *")
    initial_sync_months: Mapped[int] = mapped_column(Integer(), default=12)

