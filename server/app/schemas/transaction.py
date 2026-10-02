from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.modules.core.transaction import (
    CLASSIFICATION_RULE_VERSION,
    ClassificationSource,
    TransactionType,
    get_effective_transaction_type,
)


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

    @model_validator(mode="after")
    def resolve_classification(self) -> "TransactionRead":
        if (
            self.classification_source == ClassificationSource.RULE.value
            and self.classification_rule_version != CLASSIFICATION_RULE_VERSION
        ):
            self.transaction_type = get_effective_transaction_type(self).value
            self.classification_rule_version = CLASSIFICATION_RULE_VERSION
        return self


class TransactionSemanticUpdate(BaseModel):
    """Owner-only semantic correction; source transaction facts remain unchanged."""

    transaction_type: TransactionType
