import uuid
from enum import StrEnum

from sqlalchemy import Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ImportStatus(StrEnum):
    SUMMARY_OK = "summary_ok"
    DUPLICATE = "duplicate"
    DELETED_BY_USER = "deleted_by_user"
    FAILED = "failed"


class ImportRecord(TimestampMixin, Base):
    __tablename__ = "import_records"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    source_type: Mapped[str] = mapped_column(String(16))
    source_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    bank_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parse_status: Mapped[ImportStatus] = mapped_column(
        Enum(
            ImportStatus,
            native_enum=False,
            values_callable=lambda enum: [item.value for item in enum],
        )
    )
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
