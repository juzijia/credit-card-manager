import uuid

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ImapState(TimestampMixin, Base):
    __tablename__ = "imap_states"
    __table_args__ = (
        UniqueConstraint("account_key", "mailbox", name="uq_imap_state_account_mailbox"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # SHA-256(host, username); the mailbox address itself is not persisted.
    account_key: Mapped[str] = mapped_column(String(64), index=True)
    mailbox: Mapped[str] = mapped_column(String(255))
    uidvalidity: Mapped[int] = mapped_column(Integer())
    last_uid: Mapped[int] = mapped_column(Integer(), default=0)
    pending_uids: Mapped[str] = mapped_column(Text(), default="[]")
