from dataclasses import dataclass
from typing import Iterable

from app.models.transaction import Transaction
from app.modules.core.transaction import TransactionType, get_effective_transaction_type


@dataclass(frozen=True, slots=True)
class TransactionMetrics:
    """Canonical semantic totals for an already-selected transaction set.

    Selection/window semantics belong to the Query layer. This function owns
    the inclusion rules so Dashboard/Analytics/AnnualFee/Agents do not each
    reinvent what counts as purchase, refund, fee, repayment observation, etc.
    """

    gross_purchase_minor: int = 0
    purchase_count: int = 0
    refund_observed_minor: int = 0
    period_net_consumption_minor: int = 0
    observed_repayment_activity_minor: int = 0
    annual_fee_activity_minor: int = 0
    fee_activity_minor: int = 0
    interest_activity_minor: int = 0
    reward_activity_minor: int = 0
    cash_advance_activity_minor: int = 0
    reversal_activity_minor: int = 0
    adjustment_activity_minor: int = 0
    unknown_activity_minor: int = 0


def calculate_transaction_metrics(
    transactions: Iterable[Transaction],
) -> TransactionMetrics:
    gross_purchase_minor = 0
    purchase_count = 0
    refund_observed_minor = 0
    observed_repayment_activity_minor = 0
    annual_fee_activity_minor = 0
    fee_activity_minor = 0
    interest_activity_minor = 0
    reward_activity_minor = 0
    cash_advance_activity_minor = 0
    reversal_activity_minor = 0
    adjustment_activity_minor = 0
    unknown_activity_minor = 0

    for transaction in transactions:
        transaction_type = get_effective_transaction_type(transaction)
        amount = transaction.amount_minor

        if transaction_type == TransactionType.PURCHASE:
            gross_purchase_minor += amount
            purchase_count += 1
        elif transaction_type == TransactionType.REFUND:
            # Refund is represented as a magnitude for period consumption math.
            # Relation/lifecycle net is a different metric and is not computed here.
            refund_observed_minor += abs(amount)
        elif transaction_type == TransactionType.REPAYMENT:
            observed_repayment_activity_minor += amount
        elif transaction_type == TransactionType.ANNUAL_FEE:
            annual_fee_activity_minor += amount
        elif transaction_type == TransactionType.FEE:
            fee_activity_minor += amount
        elif transaction_type == TransactionType.INTEREST:
            interest_activity_minor += amount
        elif transaction_type == TransactionType.REWARD:
            reward_activity_minor += amount
        elif transaction_type == TransactionType.CASH_ADVANCE:
            cash_advance_activity_minor += amount
        elif transaction_type == TransactionType.REVERSAL:
            reversal_activity_minor += amount
        elif transaction_type == TransactionType.ADJUSTMENT:
            adjustment_activity_minor += amount
        else:
            unknown_activity_minor += amount

    return TransactionMetrics(
        gross_purchase_minor=gross_purchase_minor,
        purchase_count=purchase_count,
        refund_observed_minor=refund_observed_minor,
        period_net_consumption_minor=(
            gross_purchase_minor - refund_observed_minor
        ),
        observed_repayment_activity_minor=observed_repayment_activity_minor,
        annual_fee_activity_minor=annual_fee_activity_minor,
        fee_activity_minor=fee_activity_minor,
        interest_activity_minor=interest_activity_minor,
        reward_activity_minor=reward_activity_minor,
        cash_advance_activity_minor=cash_advance_activity_minor,
        reversal_activity_minor=reversal_activity_minor,
        adjustment_activity_minor=adjustment_activity_minor,
        unknown_activity_minor=unknown_activity_minor,
    )
