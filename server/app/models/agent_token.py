import uuid

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class AgentToken(TimestampMixin, Base):
    __tablename__ = "agent_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text(), nullable=True)
    scope: Mapped[str] = mapped_column(String(16))
    revoked: Mapped[bool] = mapped_column(Boolean(), default=False)
