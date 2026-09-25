import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, utc_now


class OwnerSession(TimestampMixin, Base):
    __tablename__ = "owner_sessions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    session_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime())
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(), index=True)
    revoked: Mapped[bool] = mapped_column(Boolean(), default=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)

