import pytest

from app.modules.core.transaction import (
    TransactionType,
    classify_transaction_type,
)


def test_classify_purchase_positive() -> None:
    assert classify_transaction_type(12000, "美团支付-美团外卖") == TransactionType.PURCHASE
    assert classify_transaction_type(500, "支付宝-消费-上岛咖啡") == TransactionType.PURCHASE
    assert classify_transaction_type(1500, "(消费)财付通-马小肉面馆") == TransactionType.PURCHASE
    assert classify_transaction_type(32589, "消费 抖音支付-叮咚买菜") == TransactionType.PURCHASE


def test_classify_annual_fee() -> None:
    assert classify_transaction_type(48000, "信用卡年费") == TransactionType.ANNUAL_FEE
    assert classify_transaction_type(360000, "主卡年费") == TransactionType.ANNUAL_FEE
    assert classify_transaction_type(80000, "Annual Fee Charge") == TransactionType.ANNUAL_FEE


def test_classify_interest() -> None:
    assert classify_transaction_type(1234, "循环利息") == TransactionType.INTEREST
    assert classify_transaction_type(560, "透支利息") == TransactionType.INTEREST
    assert classify_transaction_type(890, "Interest Charge") == TransactionType.INTEREST


def test_classify_fee() -> None:
    assert classify_transaction_type(1500, "分期手续费") == TransactionType.FEE
    assert classify_transaction_type(5000, "提前还款违约金") == TransactionType.FEE
    assert classify_transaction_type(300, "短信服务费") == TransactionType.FEE
    assert classify_transaction_type(2000, "Late Payment Fee") == TransactionType.FEE


def test_classify_failed_repayment_chargeback_never_purchase() -> None:
    result1 = classify_transaction_type(97526, "还款失败 败－其他")
    assert result1 in (TransactionType.ADJUSTMENT, TransactionType.REVERSAL)
    assert result1 != TransactionType.PURCHASE

    result2 = classify_transaction_type(10000, "自动还款失败冲回")
    assert result2 in (TransactionType.ADJUSTMENT, TransactionType.REVERSAL)
    assert result2 != TransactionType.PURCHASE

    result3 = classify_transaction_type(5000, "代扣还款冲正")
    assert result3 in (TransactionType.ADJUSTMENT, TransactionType.REVERSAL)
    assert result3 != TransactionType.PURCHASE


def test_classify_refund_negative() -> None:
    assert classify_transaction_type(-2000, "美团退款") == TransactionType.REFUND
    assert classify_transaction_type(-89, "退货 抖音支付-叮咚买菜") == TransactionType.REFUND
    assert classify_transaction_type(-1500, "商户退款-天猫商城") == TransactionType.REFUND
    assert classify_transaction_type(-500, "Tax Refund") == TransactionType.REFUND


def test_classify_reward_requires_explicit_reward_semantics() -> None:
    assert classify_transaction_type(-2000, "活动返现奖励金") == TransactionType.REWARD
    assert classify_transaction_type(-500, "Statement credit") == TransactionType.REFUND


def test_classify_repayment_negative() -> None:
    assert classify_transaction_type(-10000, "信用卡还款") == TransactionType.REPAYMENT
    assert classify_transaction_type(-5176, "还款成功-网上还款，谢谢 !") == TransactionType.REPAYMENT
    assert classify_transaction_type(-8740, "预约还款") == TransactionType.REPAYMENT
    assert classify_transaction_type(-4005, "人民币自动还款") == TransactionType.REPAYMENT
    assert classify_transaction_type(-20380, "(还款)支付宝-还款") == TransactionType.REPAYMENT
    assert classify_transaction_type(-5000, "跨行代付 银联转账") == TransactionType.REPAYMENT
    assert classify_transaction_type(-135200, "江苏 跨行代付 恒丰银行") == TransactionType.REPAYMENT
    assert classify_transaction_type(-50000, "福建 跨行代付 （特约）厦门平安（京") == TransactionType.REPAYMENT
    assert classify_transaction_type(-50000, "银联入账 银联转账") == TransactionType.REPAYMENT
    assert classify_transaction_type(-19855, "无法足额扣款，请补足账户余额") == TransactionType.REPAYMENT
    assert classify_transaction_type(-2100, "代理业务转帐 账务中心") == TransactionType.REPAYMENT
    assert classify_transaction_type(-10000, "微信还款-招商银行") == TransactionType.REPAYMENT
    assert classify_transaction_type(-20000, "天翼支付客户还信用卡 天翼支付") == TransactionType.REPAYMENT
    assert classify_transaction_type(-5000, "Bank of China Mobile Cl ient CHN") == TransactionType.REPAYMENT
    assert classify_transaction_type(-4000, "BOCNET CHN") == TransactionType.REPAYMENT
    assert classify_transaction_type(-127158, "卡卡转账 某种备注") == TransactionType.REPAYMENT


def test_classify_negative_without_sufficient_semantics() -> None:
    assert classify_transaction_type(-1400, "钱袋宝支付（美团平台商户）CHN") == TransactionType.REFUND
    assert classify_transaction_type(-500, "普通无备注负向入账") == TransactionType.REFUND


def test_classify_cash_advance() -> None:
    assert classify_transaction_type(200000, "ATM取现") == TransactionType.CASH_ADVANCE
    assert classify_transaction_type(50000, "预借现金") == TransactionType.CASH_ADVANCE
    assert classify_transaction_type(100000, "透支取现") == TransactionType.CASH_ADVANCE


def test_classify_empty_description() -> None:
    assert classify_transaction_type(1000, "") == TransactionType.PURCHASE
    assert classify_transaction_type(1000, "   ") == TransactionType.PURCHASE
    assert classify_transaction_type(1000, None) == TransactionType.PURCHASE

    assert classify_transaction_type(-1000, "") == TransactionType.REFUND
    assert classify_transaction_type(-1000, "   ") == TransactionType.REFUND
    assert classify_transaction_type(-1000, None) == TransactionType.REFUND

    assert classify_transaction_type(0, "") == TransactionType.UNKNOWN
    assert classify_transaction_type(0, "无金额交易") == TransactionType.UNKNOWN


def test_classify_case_and_whitespace_insensitivity() -> None:
    assert classify_transaction_type(1000, "  annual   fee  ") == TransactionType.ANNUAL_FEE
    assert classify_transaction_type(500, "INTEREST CHARGE") == TransactionType.INTEREST
    assert classify_transaction_type(-200, "  REFUND   ") == TransactionType.REFUND
    assert classify_transaction_type(97526, "  还款失败  ") == TransactionType.ADJUSTMENT


def test_classify_installment_cancellation_and_adjustment() -> None:
    assert classify_transaction_type(
        60140,
        "分期扣款 1/6期-商户分期-网银在线-京东商城业务"
    ) == TransactionType.ADJUSTMENT

    assert classify_transaction_type(
        360840,
        "分期付款取消 退货分期调整-商户分期-网银在线-京东商城业务"
    ) == TransactionType.ADJUSTMENT

    assert classify_transaction_type(
        300700,
        "分期付款取消 退货分期调整-商户分期-网银在线-京东商城业务"
    ) == TransactionType.ADJUSTMENT

    assert classify_transaction_type(
        -360840,
        "退货 网银在线-京东（银联信用卡分期）-京东商城业务"
    ) == TransactionType.REFUND

    assert classify_transaction_type(
        360840,
        "消费 商户分期-网银在线-京东商城业务-京东商城业务-分6期"
    ) == TransactionType.PURCHASE
