"""Public entry point for the core Transaction module.

Other modules should import transaction behavior from here instead of reaching
into implementation files.
"""

from app.modules.core.transaction.classification import (
    CLASSIFICATION_RULE_VERSION,
    ClassificationSource,
    TransactionClassification,
    TransactionType,
    apply_rule_classification,
    classify_transaction,
    classify_transaction_type,
)
from app.modules.core.transaction.lifecycle import (
    LifecycleViolationError,
    calculate_statement_activity,
    calculate_transaction_net_amount,
    get_effective_date,
    get_effective_transaction_type,
    remaining_refundable_minor,
    validate_and_link_refund,
    validate_purchase_amount_update,
    validate_purchase_deletion,
    validate_purchase_type_update,
)
from app.modules.core.transaction.queries import list_transactions

__all__ = [
    "CLASSIFICATION_RULE_VERSION",
    "ClassificationSource",
    "LifecycleViolationError",
    "TransactionClassification",
    "TransactionType",
    "apply_rule_classification",
    "calculate_statement_activity",
    "calculate_transaction_net_amount",
    "classify_transaction",
    "classify_transaction_type",
    "get_effective_date",
    "get_effective_transaction_type",
    "list_transactions",
    "remaining_refundable_minor",
    "validate_and_link_refund",
    "validate_purchase_amount_update",
    "validate_purchase_deletion",
    "validate_purchase_type_update",
]
