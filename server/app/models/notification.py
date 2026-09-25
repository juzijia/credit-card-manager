import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, utc_now


class NotificationSetting(TimestampMixin, Base):
    __tablename__ = "notification_settings"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    card_id: Mapped[str | None] = mapped_column(
        ForeignKey("cards.id", ondelete="CASCADE"), nullable=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean(), default=False)
    offset_days_json: Mapped[str] = mapped_column(String(64), default="[2, 1, 0, -1, -2]")
    push_time: Mapped[str] = mapped_column(String(16), default="09:00")
    provider: Mapped[str] = mapped_column(String(32), default="disabled")
    public_config_json: Mapped[str] = mapped_column(Text(), default="{}")
    secret_ciphertext: Mapped[str | None] = mapped_column(Text(), nullable=True)
    only_if_unpaid: Mapped[bool] = mapped_column(Boolean(), default=True)


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (
        UniqueConstraint(
            "statement_id",
            "offset_days",
            "provider",
            name="uq_notification_statement_offset_provider",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    statement_id: Mapped[str] = mapped_column(
        ForeignKey("statements.id", ondelete="CASCADE"), index=True
    )
    offset_days: Mapped[int] = mapped_column(Integer(), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    sent_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    status: Mapped[str] = mapped_column(String(16), default="success")
    response_text: Mapped[str | None] = mapped_column(Text(), nullable=True)

