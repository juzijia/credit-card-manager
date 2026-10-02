import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


CLASSIFICATION_RULE_VERSION = "2026-10-02-v3"


class TransactionType(StrEnum):
    PURCHASE = "purchase"
    REFUND = "refund"
    REPAYMENT = "repayment"
    ANNUAL_FEE = "annual_fee"
    INTEREST = "interest"
    FEE = "fee"
    REVERSAL = "reversal"
    ADJUSTMENT = "adjustment"
    CASH_ADVANCE = "cash_advance"
    REWARD = "reward"
    UNKNOWN = "unknown"


class ClassificationSource(StrEnum):
    RULE = "rule"
    MANUAL = "manual"
    RELATION = "relation"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class TransactionClassification:
    transaction_type: TransactionType
    source: ClassificationSource = ClassificationSource.RULE
    rule_version: str | None = CLASSIFICATION_RULE_VERSION


_RE_ADJUSTMENT = re.compile(
    r"(还款失败|扣款失败|代扣失败|转账失败|扣款冲回|还款冲回|调账|账务调整|错账|分期扣款|分期付款取消|退货分期调整|adjustment)",
    re.IGNORECASE,
)
_RE_REVERSAL = re.compile(r"(冲正|冲销|撤销|冲回|reversal)", re.IGNORECASE)
_RE_ANNUAL_FEE = re.compile(r"(年费|annual\s*fee)", re.IGNORECASE)
_RE_INTEREST = re.compile(
    r"(利息|循环利息|透支利息|息费|finance\s*charge|interest)", re.IGNORECASE
)
_RE_FEE = re.compile(
    r"(手续费|分期手续费|取现手续费|服务费|滞纳金|违约金|超限费|工本费|挂失费|换卡费|短信费|账户管理费|\bfee\b|\bcharges?\b)",
    re.IGNORECASE,
)
_RE_REPAYMENT = re.compile(
    r"(还款|约还|约定还款|自动还款|预约还款|信用卡还款|还信用卡|还款成功|自扣还款|代扣还款|网银还款|转[账帐]还款|跨行汇款|银联跨行|"
    r"微信.*还款|支付宝.*还款|天翼支付.*还信用卡|"
    r"代扣|代付|跨行代付|转入|存入|打款|转[账帐]|汇款|银联代扣|银联转[账帐]|银联入账|实时付款入账|"
    r"无法足额扣款|谢谢|多谢|感谢|存款|溢缴|bocnet|mobile\s*cl\s*ient|repayment|payment\s*received)",
    re.IGNORECASE,
)
_RE_REFUND = re.compile(r"(退款|退货|退费|退税|退保|退付|冲抵|refund|return)", re.IGNORECASE)
_RE_REWARD = re.compile(r"(返现|刷卡金|奖励金|活动奖励|积分兑换|返利|cashback|reward)", re.IGNORECASE)
_RE_CASH_ADVANCE = re.compile(
    r"(预借现金|取现|取款|现金分期|透支取现|atm取现|cash\s*advance|atm\s*withdrawal)",
    re.IGNORECASE,
)


def classify_transaction_type(amount_minor: int, raw_description: str | None) -> TransactionType:
    desc = " ".join((raw_description or "").strip().split())

    if not desc:
        if amount_minor > 0:
            return TransactionType.PURCHASE
        if amount_minor < 0:
            return TransactionType.REFUND
        return TransactionType.UNKNOWN
    if _RE_ADJUSTMENT.search(desc):
        return TransactionType.ADJUSTMENT
    if _RE_REVERSAL.search(desc):
        return TransactionType.REVERSAL
    if _RE_ANNUAL_FEE.search(desc):
        return TransactionType.ANNUAL_FEE
    if _RE_INTEREST.search(desc):
        return TransactionType.INTEREST
    if _RE_FEE.search(desc):
        return TransactionType.FEE
    if amount_minor < 0 and _RE_REPAYMENT.search(desc):
        return TransactionType.REPAYMENT
    if _RE_REFUND.search(desc):
        return TransactionType.REFUND
    if _RE_REWARD.search(desc):
        return TransactionType.REWARD
    if _RE_CASH_ADVANCE.search(desc):
        return TransactionType.CASH_ADVANCE
    if amount_minor > 0:
        return TransactionType.PURCHASE
    if amount_minor < 0:
        return TransactionType.REFUND
    return TransactionType.UNKNOWN


def classify_transaction(amount_minor: int, raw_description: str | None) -> TransactionClassification:
    return TransactionClassification(
        transaction_type=classify_transaction_type(amount_minor, raw_description)
    )


def apply_rule_classification(transaction: Any, *, force: bool = False) -> TransactionType:
    current_source = getattr(transaction, "classification_source", None)
    if current_source == ClassificationSource.MANUAL.value and not force:
        raw_type = getattr(transaction, "transaction_type", None)
        try:
            return TransactionType(raw_type)
        except (TypeError, ValueError):
            return TransactionType.UNKNOWN

    result = classify_transaction(
        getattr(transaction, "amount_minor", 0),
        getattr(transaction, "raw_description", None),
    )
    transaction.transaction_type = result.transaction_type.value
    transaction.classification_source = result.source.value
    transaction.classification_rule_version = result.rule_version
    return result.transaction_type
