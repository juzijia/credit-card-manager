from types import SimpleNamespace

from app.services.financial_analysis.metrics import calculate_transaction_metrics


def _tx(amount_minor: int, transaction_type: str):
    return SimpleNamespace(
        amount_minor=amount_minor,
        transaction_type=transaction_type,
        raw_description="",
    )


def test_transaction_metrics_keep_semantic_categories_separate() -> None:
    metrics = calculate_transaction_metrics(
        [
            _tx(10_000, "purchase"),
            _tx(5_000, "purchase"),
            _tx(-2_000, "refund"),
            _tx(-10_000, "repayment"),
            _tx(48_000, "annual_fee"),
            _tx(500, "fee"),
            _tx(300, "interest"),
            _tx(-1_000, "reward"),
            _tx(2_000, "cash_advance"),
            _tx(-5_000, "reversal"),
            _tx(100, "adjustment"),
            _tx(-123, "unknown"),
        ]
    )

    assert metrics.gross_purchase_minor == 15_000
    assert metrics.purchase_count == 2
    assert metrics.refund_observed_minor == 2_000
    assert metrics.period_net_consumption_minor == 13_000
    assert metrics.observed_repayment_activity_minor == -10_000
    assert metrics.annual_fee_activity_minor == 48_000
    assert metrics.fee_activity_minor == 500
    assert metrics.interest_activity_minor == 300
    assert metrics.reward_activity_minor == -1_000
    assert metrics.cash_advance_activity_minor == 2_000
    assert metrics.reversal_activity_minor == -5_000
    assert metrics.adjustment_activity_minor == 100
    assert metrics.unknown_activity_minor == -123


def test_refund_only_period_can_have_negative_net_consumption() -> None:
    metrics = calculate_transaction_metrics([_tx(-10_000, "refund")])
    assert metrics.gross_purchase_minor == 0
    assert metrics.refund_observed_minor == 10_000
    assert metrics.period_net_consumption_minor == -10_000


def test_installment_cancellation_seven_row_regression() -> None:
    stmt_a_txs = [
        _tx(-360840, "refund"),
        _tx(60140, "adjustment"),
        _tx(360840, "purchase"),
        _tx(360840, "purchase"),
        _tx(360840, "adjustment"),
    ]

    stmt_b_txs = [
        _tx(-360840, "refund"),
        _tx(300700, "adjustment"),
    ]

    metrics_a = calculate_transaction_metrics(stmt_a_txs)
    assert metrics_a.gross_purchase_minor == 721680
    assert metrics_a.refund_observed_minor == 360840
    assert metrics_a.adjustment_activity_minor == 420980
    assert metrics_a.period_net_consumption_minor == 360840

    metrics_b = calculate_transaction_metrics(stmt_b_txs)
    assert metrics_b.gross_purchase_minor == 0
    assert metrics_b.refund_observed_minor == 360840
    assert metrics_b.adjustment_activity_minor == 300700
    assert metrics_b.period_net_consumption_minor == -360840

    all_seven_txs = stmt_a_txs + stmt_b_txs
    metrics_all = calculate_transaction_metrics(all_seven_txs)
    assert metrics_all.period_net_consumption_minor == 0
