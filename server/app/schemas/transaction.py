from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.modules.core.transaction import TransactionType


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    statement_id: str
    transaction_date: date | None
    posting_date: date | None
    amount_minor: int
    currency: str
    raw_description: str
    merchant: str | None
    channel: str | None
    category: str | None
    external_ref: str | None
    transaction_type: str | None = None
    classification_source: str | None = None
    classification_rule_version: str | None = None
    original_transaction_id: str | None = None
    card_id: str | None = None
    created_at: datetime


class TransactionSemanticUpdate(BaseModel):
    """Owner-only semantic correction; source transaction facts remain unchanged."""

    transaction_type: TransactionType
