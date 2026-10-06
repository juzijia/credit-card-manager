"""Transaction lifecycle contracts exercised through production helpers."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.models.statement import Statement
from app.models.transaction import Transaction
from app.modules.core.transaction import (
    LifecycleViolationError,
    TransactionType,
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


def _make_tx(
    tx_id: str,
    amount_minor: int,
    description: str,
    tx_type: str | None = None,
    tx_date: Any = ...,
    posting_date: Any = ...,
    currency: str = "CNY",
) -> Transaction:
    transaction = Transaction(
        id=tx_id,
        statement_id="stmt-test-1",
        transaction_date=date(2026, 8, 10) if tx_date is ... else tx_date,
        posting_date=date(2026, 8, 12) if posting_date is ... else posting_date,
        amount_minor=amount_minor,
        currency=currency,
        raw_description=description,
        transaction_type=tx_type,
    )
    transaction.refunds = []
    return transaction


def _statement(statement_id: str, transactions: list[Transaction]) -> Statement:
    statement = Statement(
        bank_code="CMB",
        id=statement_id,
        card_id="card-1",
        statement_date=date(2026, 8, 25),
        due_date=date(2026, 9, 15),
        total_due_minor=0,
        currency="CNY",
    )
    statement.transactions = transactions
    return statement


def test_gt_001_purchase_net_and_statement_activity() -> None:
    purchase = _make_tx("purchase", 10_000, "普通消费", "purchase")

    assert calculate_transaction_net_amount(purchase) == 10_000
    assert calculate_statement_activity(_statement("statement", [purchase])) == 10_000


def test_gt_002_full_refund_keeps_original_fact_and_reduces_net_to_zero() -> None:
    purchase = _make_tx("purchase", 10_000, "普通消费", "purchase")
    refund = _make_tx("refund", -10_000, "全额退款", "refund")

    validate_and_link_refund(purchase, refund)

    assert purchase.amount_minor == 10_000
    assert refund.amount_minor == -10_000
    assert refund.original_transaction_id == purchase.id
    assert calculate_transaction_net_amount(purchase) == 0
    assert remaining_refundable_minor(purchase) == 0


def test_gt_003_cross_cycle_refund_preserves_each_period_sign() -> None:
    purchase = _make_tx(
        "purchase", 10_000, "8月消费", "purchase", date(2026, 8, 10)
    )
    refund = _make_tx(
        "refund", -10_000, "9月退款", "refund", date(2026, 9, 5)
    )
    august = _statement("august", [purchase])
    september = _statement("september", [refund])

    validate_and_link_refund(purchase, refund)

    assert calculate_transaction_net_amount(purchase) == 0
    assert calculate_statement_activity(august) == 10_000
    assert calculate_statement_activity(september) == -10_000


def test_gt_004_multiple_partial_refunds_are_one_to_many() -> None:
    purchase = _make_tx("purchase", 10_000, "普通消费", "purchase")
    first = _make_tx("refund-1", -3_000, "第一次退款", "refund")
    second = _make_tx("refund-2", -2_000, "第二次退款", "refund")

    validate_and_link_refund(purchase, first)
    validate_and_link_refund(purchase, second)

    assert [refund.id for refund in purchase.refunds] == ["refund-1", "refund-2"]
    assert calculate_transaction_net_amount(purchase) == 5_000
    assert remaining_refundable_minor(purchase) == 5_000


def test_gt_005_over_refund_is_rejected_without_mutating_raw_amount() -> None:
    purchase = _make_tx("purchase", 10_000, "普通消费", "purchase")
    refund = _make_tx("refund", -12_000, "异常贷记", "refund")

    with pytest.raises(LifecycleViolationError, match="exceeds remaining refundable"):
        validate_and_link_refund(purchase, refund)

    assert refund.amount_minor == -12_000
    assert refund.original_transaction_id is None
    assert purchase.refunds == []


@pytest.mark.parametrize(
    ("original_type", "refund_amount", "message"),
    [
        ("fee", -1_000, "non-purchase"),
        ("purchase", 1_000, "must have negative amount"),
    ],
)
def test_gt_006_refund_link_requires_purchase_and_negative_refund(
    original_type: str,
    refund_amount: int,
    message: str,
) -> None:
    original = _make_tx("original", 10_000, "原交易", original_type)
    refund = _make_tx("refund", refund_amount, "退款", "refund")

    with pytest.raises(LifecycleViolationError, match=message):
        validate_and_link_refund(original, refund)


def test_gt_007_cross_currency_refund_is_rejected() -> None:
    purchase = _make_tx("purchase", 10_000, "国内消费", "purchase")
    refund = _make_tx("refund", -1_500, "境外退款", "refund", currency="USD")

    with pytest.raises(LifecycleViolationError, match="Currency mismatch"):
        validate_and_link_refund(purchase, refund)


def test_gt_008_purchase_update_cannot_fall_below_existing_refunds() -> None:
    purchase = _make_tx("purchase", 10_000, "普通消费", "purchase")
    refund = _make_tx("refund", -7_000, "部分退款", "refund")
    validate_and_link_refund(purchase, refund)

    with pytest.raises(LifecycleViolationError, match="below already refunded total"):
        validate_purchase_amount_update(purchase, 5_000)

    assert validate_purchase_amount_update(purchase, 8_000) == 1_000


def test_gt_009_purchase_with_refunds_cannot_change_type_or_be_deleted() -> None:
    purchase = _make_tx("purchase", 10_000, "普通消费", "purchase")
    refund = _make_tx("refund", -3_000, "部分退款", "refund")
    validate_and_link_refund(purchase, refund)

    with pytest.raises(LifecycleViolationError, match="has 1 active linked refunds"):
        validate_purchase_type_update(purchase, TransactionType.FEE)
    with pytest.raises(LifecycleViolationError, match="has 1 active linked refunds"):
        validate_purchase_deletion(purchase)

    validate_purchase_type_update(purchase, TransactionType.PURCHASE)


def test_gt_010_cross_card_refund_is_rejected() -> None:
    purchase_statement = _statement("purchase-statement", [])
    purchase_statement.card_id = "card-a"
    refund_statement = _statement("refund-statement", [])
    refund_statement.card_id = "card-b"
    purchase = _make_tx("purchase", 10_000, "普通消费", "purchase")
    refund = _make_tx("refund", -3_000, "部分退款", "refund")
    purchase.statement = purchase_statement
    refund.statement = refund_statement

    with pytest.raises(LifecycleViolationError, match="Cross-card refund linkage rejected"):
        validate_and_link_refund(purchase, refund)


def test_gt_011_effective_date_prefers_transaction_date_then_posting_date() -> None:
    transaction_only = _make_tx(
        "transaction-only", -3_000, "退款", "refund", date(2026, 8, 15), None
    )
    posting_only = _make_tx(
        "posting-only", -3_000, "退款", "refund", None, date(2026, 8, 18)
    )
    both = _make_tx(
        "both",
        -3_000,
        "退款",
        "refund",
        date(2026, 8, 15),
        date(2026, 8, 18),
    )

    assert get_effective_date(transaction_only) == date(2026, 8, 15)
    assert get_effective_date(posting_only) == date(2026, 8, 18)
    assert get_effective_date(both) == date(2026, 8, 15)


def test_gt_012_legacy_null_types_use_conservative_classification() -> None:
    assert get_effective_transaction_type(
        _make_tx("purchase", 10_000, "普通消费")
    ) == TransactionType.PURCHASE
    assert get_effective_transaction_type(
        _make_tx("fee", 3_000, "信用卡年费")
    ) == TransactionType.ANNUAL_FEE
    assert get_effective_transaction_type(
        _make_tx("refund", -2_000, "商户退款")
    ) == TransactionType.REFUND
    assert get_effective_transaction_type(
        _make_tx("repayment", -10_000, "支付宝还款")
    ) == TransactionType.REPAYMENT
