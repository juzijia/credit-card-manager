"""Transaction lifecycle helpers kept behind the Transaction module boundary.

This file preserves current behavior during modularization. Business simplification
can happen later without changing API callers outside the module.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from app.modules.core.transaction.classification import (
    ClassificationSource,
    TransactionType,
    classify_transaction_type,
)

if TYPE_CHECKING:
    from app.models.statement import Statement
    from app.models.transaction import Transaction


class LifecycleViolationError(ValueError):
    pass


def get_effective_transaction_type(transaction: Transaction | Any) -> TransactionType:
    raw_type = getattr(transaction, "transaction_type", None)
    if raw_type:
        try:
            return TransactionType(raw_type)
        except ValueError:
            pass
    return classify_transaction_type(
        getattr(transaction, "amount_minor", 0),
        getattr(transaction, "raw_description", ""),
    )


def get_effective_date(transaction: Transaction | Any) -> date | None:
    return getattr(transaction, "transaction_date", None) or getattr(
        transaction, "posting_date", None
    )


def remaining_refundable_minor(original: Transaction | Any) -> int:
    amount = getattr(original, "amount_minor", 0)
    if amount <= 0:
        return 0
    refunds = getattr(original, "refunds", None) or []
    total_refunded = sum(abs(getattr(r, "amount_minor", 0)) for r in refunds)
    return max(0, amount - total_refunded)


def calculate_transaction_net_amount(original: Transaction | Any) -> int:
    amount = getattr(original, "amount_minor", 0)
    if amount <= 0:
        return amount
    refunds = getattr(original, "refunds", None) or []
    total_refunded = sum(abs(getattr(r, "amount_minor", 0)) for r in refunds)
    return max(0, amount - total_refunded)


def validate_and_link_refund(original: Transaction, refund: Transaction) -> int:
    orig_type = get_effective_transaction_type(original)
    if orig_type != TransactionType.PURCHASE or original.amount_minor <= 0:
        raise LifecycleViolationError(
            f"Cannot link refund to non-purchase transaction (id={original.id}, type={orig_type.value})"
        )

    orig_card_id = getattr(getattr(original, "statement", None), "card_id", None) or getattr(original, "card_id", None)
    refund_card_id = getattr(getattr(refund, "statement", None), "card_id", None) or getattr(refund, "card_id", None)
    if orig_card_id is not None and refund_card_id is not None and orig_card_id != refund_card_id:
        raise LifecycleViolationError(
            f"Cross-card refund linkage rejected: purchase belongs to card {orig_card_id}, refund belongs to card {refund_card_id}"
        )

    if refund.amount_minor >= 0:
        raise LifecycleViolationError(
            f"Refund transaction must have negative amount (got {refund.amount_minor})"
        )
    if original.currency != refund.currency:
        raise LifecycleViolationError(
            f"Currency mismatch: original has {original.currency}, refund has {refund.currency}"
        )

    available = remaining_refundable_minor(original)
    requested_refund_mag = abs(refund.amount_minor)
    if requested_refund_mag > available:
        raise LifecycleViolationError(
            f"Refund amount {requested_refund_mag} exceeds remaining refundable {available}"
        )

    refund.original_transaction_id = original.id
    refund.transaction_type = TransactionType.REFUND.value
    refund.classification_source = ClassificationSource.RELATION.value
    refund.classification_rule_version = None
    if original.refunds is not None and refund not in original.refunds:
        original.refunds.append(refund)
    return requested_refund_mag


def validate_purchase_amount_update(original: Transaction, new_amount_minor: int) -> int:
    if new_amount_minor <= 0:
        raise LifecycleViolationError(
            f"Purchase amount must be positive, got {new_amount_minor}"
        )
    refunds = getattr(original, "refunds", None) or []
    total_refunded = sum(abs(getattr(r, "amount_minor", 0)) for r in refunds)
    if new_amount_minor < total_refunded:
        raise LifecycleViolationError(
            f"Cannot reduce purchase amount to {new_amount_minor} minor units: below already refunded total of {total_refunded} minor units"
        )
    return new_amount_minor - total_refunded


def validate_purchase_type_update(original: Transaction, new_type: TransactionType | str) -> None:
    val = new_type.value if hasattr(new_type, "value") else str(new_type)
    refunds = getattr(original, "refunds", None) or []
    if refunds and val != TransactionType.PURCHASE.value:
        raise LifecycleViolationError(
            f"Cannot change transaction_type to {val} on purchase id={getattr(original, 'id', None)}: has {len(refunds)} active linked refunds"
        )


def validate_purchase_deletion(original: Transaction) -> None:
    refunds = getattr(original, "refunds", None) or []
    if refunds:
        raise LifecycleViolationError(
            f"Cannot delete purchase id={getattr(original, 'id', None)}: has {len(refunds)} active linked refunds"
        )


def calculate_statement_activity(statement: Statement | Any) -> int:
    transactions = getattr(statement, "transactions", None) or []
    return sum(getattr(tx, "amount_minor", 0) for tx in transactions)
