from calendar import monthrange
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.manual_bill import ManualBill, ManualBillPayment
from app.modules.core.transaction import get_effective_date
from app.schemas.analytics import (
    AnalyticsCoverage,
    AnalyticsPeriod,
    AnalyticsResponse,
    BankUtilization,
    CardUtilization,
    MonthlyBankUsage,
    MonthlyConsumption,
    MonthlyDue,
    MonthlyRepayment,
)
from app.services.financial_analysis.metrics import calculate_transaction_metrics
from app.services.financial_analysis.queries import (
    evaluate_calendar_coverage,
    list_active_statements,
    list_calendar_statements,
    list_calendar_transactions,
    list_statement_window_transactions,
    list_visible_cards,
    list_visible_registered_repayments,
    ratio_percent,
)
from app.services.statements import local_today, remaining_minor
from app.services.system_settings import get_history_cutoff_date


def _add_months(value: date, offset: int) -> date:
    absolute_month = value.year * 12 + value.month - 1 + offset
    return date(absolute_month // 12, absolute_month % 12 + 1, 1)


def _month_key(value: date | datetime, timezone_name: str | None = None) -> str:
    if isinstance(value, datetime) and timezone_name:
        value = value.replace(tzinfo=UTC).astimezone(ZoneInfo(timezone_name))
    return value.strftime("%Y-%m")


def _legacy_nonnegative_ratio(value: float | None) -> float | None:
    return max(0.0, value) if value is not None else None


def _monthly_bank_usage(
    *,
    bank_code: str,
    bank_name: str,
    amount_minor: int,
    credit_limit_minor: int | None,
) -> MonthlyBankUsage:
    signed_ratio = ratio_percent(amount_minor, credit_limit_minor)
    return MonthlyBankUsage(
        bank_code=bank_code,
        bank_name=bank_name,
        amount_minor=amount_minor,
        credit_limit_minor=credit_limit_minor,
        monthly_spend_to_limit_percent=signed_ratio,
        utilization_percent=_legacy_nonnegative_ratio(signed_ratio),
    )


def build_analytics(
    session: Session, months: int, *, repayment_card_id: str | None = None
) -> AnalyticsResponse:
    timezone_name = session.info["timezone"]
    today = local_today(timezone_name)
    end_month = date(today.year, today.month, 1)
    start_month = _add_months(end_month, -(months - 1))
    end_date = date(today.year, today.month, monthrange(today.year, today.month)[1])
    month_keys = [
        _add_months(start_month, offset).strftime("%Y-%m")
        for offset in range(months)
    ]

    cards = list_visible_cards(session, currency="CNY")
    bank_limits: dict[tuple[str, str], int | None] = {}
    bank_code_to_key: dict[str, tuple[str, str]] = {}
    for card in cards:
        bank_key = (card.bank_code, card.bank_name)
        bank_code_to_key.setdefault(card.bank_code, bank_key)
        if bank_key not in bank_limits:
            bank_limits[bank_key] = card.credit_limit_minor
        elif card.credit_limit_minor is not None:
            bank_limits[bank_key] = max(
                bank_limits[bank_key] or 0, card.credit_limit_minor
            )

    transactions = list_calendar_transactions(
        session,
        start_date=start_month,
        end_date=end_date,
        currency="CNY",
    )
    month_transactions = {key: [] for key in month_keys}
    bank_month_transactions = {
        key: {bank_key: [] for bank_key in bank_limits} for key in month_keys
    }
    for transaction in transactions:
        tx_date = get_effective_date(transaction)
        if not tx_date:
            continue
        key = _month_key(tx_date)
        if key not in month_transactions:
            continue
        month_transactions[key].append(transaction)

        card = transaction.statement.card
        if card is not None:
            bank_key = (card.bank_code, card.bank_name)
        else:
            bank_key = bank_code_to_key.get(transaction.statement.bank_code)
        if bank_key is not None and bank_key in bank_month_transactions[key]:
            bank_month_transactions[key][bank_key].append(transaction)

    month_metrics = {
        key: calculate_transaction_metrics(month_transactions[key]) for key in month_keys
    }
    bank_month_metrics = {
        key: {
            bank_key: calculate_transaction_metrics(
                bank_month_transactions[key][bank_key]
            )
            for bank_key in bank_limits
        }
        for key in month_keys
    }
    cutoff = get_history_cutoff_date(session)
    active_statements = list_active_statements(
        session,
        cutoff=cutoff,
        currency="CNY",
    )
    statement_due = {key: 0 for key in month_keys}
    outstanding_by_card: dict[str, int] = {}
    account_outstanding_by_bank: dict[str, int] = {}
    for statement in active_statements:
        remaining = remaining_minor(statement)
        if statement.card_id is None:
            account_outstanding_by_bank[statement.bank_code] = (
                account_outstanding_by_bank.get(statement.bank_code, 0) + remaining
            )
        else:
            outstanding_by_card[statement.card_id] = (
                outstanding_by_card.get(statement.card_id, 0) + remaining
            )
        key = _month_key(statement.due_date)
        if key in statement_due:
            statement_due[key] += remaining

    manual_due = {key: 0 for key in month_keys}
    manual_consumption = {key: {"amount_minor": 0, "count": 0} for key in month_keys}
    manual_payments = session.scalars(
        select(ManualBillPayment)
        .join(ManualBill, ManualBill.id == ManualBillPayment.manual_bill_id)
        .where(
            ManualBill.enabled.is_(True),
            ManualBillPayment.due_date >= start_month,
            ManualBillPayment.due_date <= end_date,
        )
    ).all()
    for payment in manual_payments:
        key = _month_key(payment.due_date)
        if payment.paid_at is None and key in manual_due:
            manual_due[key] += payment.amount_minor
        if key in manual_consumption:
            manual_consumption[key]["amount_minor"] += payment.amount_minor
            manual_consumption[key]["count"] += 1

    card_utilization = []
    for card in cards:
        outstanding = outstanding_by_card.get(card.id, 0)
        ratio = ratio_percent(outstanding, card.credit_limit_minor)
        card_utilization.append(
            CardUtilization(
                card_id=card.id,
                bank_code=card.bank_code,
                bank_name=card.bank_name,
                display_name=card.display_name,
                last4=card.last4,
                outstanding_minor=outstanding,
                credit_limit_minor=card.credit_limit_minor,
                outstanding_to_known_limit_percent=ratio,
                utilization_percent=ratio,
            )
        )

    bank_values: dict[tuple[str, str], dict[str, int | str | None]] = {}
    for card in cards:
        if card.deleted_at is not None or card.status != "active":
            continue
        key = (card.bank_code, card.bank_name)
        current = bank_values.setdefault(
            key,
            {
                "outstanding_minor": 0,
                "credit_limit_minor": None,
            },
        )
        current["outstanding_minor"] = int(current["outstanding_minor"] or 0) + (
            outstanding_by_card.get(card.id, 0)
        )
        if card.credit_limit_minor is not None:
            current["credit_limit_minor"] = max(
                int(current["credit_limit_minor"] or 0), card.credit_limit_minor
            )
    bank_utilization = []
    for (bank_code, bank_name), values in bank_values.items():
        outstanding = int(values["outstanding_minor"] or 0) + account_outstanding_by_bank.get(
            bank_code, 0
        )
        limit = (
            int(values["credit_limit_minor"])
            if values["credit_limit_minor"] is not None
            else None
        )
        ratio = ratio_percent(outstanding, limit)
        bank_utilization.append(
            BankUtilization(
                bank_code=bank_code,
                bank_name=bank_name,
                outstanding_minor=outstanding,
                credit_limit_minor=limit,
                outstanding_to_known_limit_percent=ratio,
                utilization_percent=ratio,
            )
        )
    bank_utilization.sort(key=lambda item: (-item.outstanding_minor, item.bank_name))
    card_utilization.sort(key=lambda item: (-item.outstanding_minor, item.bank_name))

    repayments = {
        key: {"credit_card_paid_minor": 0, "manual_bill_paid_minor": 0}
        for key in month_keys
    }
    card_repayments = list_visible_registered_repayments(session)
    for repayment in card_repayments:
        if repayment_card_id is not None and repayment.statement.card_id != repayment_card_id:
            continue
        key = _month_key(repayment.paid_at, timezone_name)
        if key in repayments:
            repayments[key]["credit_card_paid_minor"] += repayment.amount_minor
    manual_repayments = session.scalars(
        select(ManualBillPayment)
        .join(ManualBill, ManualBill.id == ManualBillPayment.manual_bill_id)
        .where(ManualBill.enabled.is_(True), ManualBillPayment.paid_at.is_not(None))
    ).all()
    for payment in manual_repayments:
        if payment.paid_at is None:
            continue
        key = _month_key(payment.paid_at, timezone_name)
        if key in repayments:
            repayments[key]["manual_bill_paid_minor"] += payment.amount_minor

    coverage_statements = list_calendar_statements(
        session,
        start_date=start_month,
        end_date=end_date,
        currency="CNY",
    )
    coverage_transactions = list_statement_window_transactions(
        session,
        start_date=start_month,
        end_date=end_date,
        currency="CNY",
    )
    coverage = evaluate_calendar_coverage(
        cards=cards,
        statements=coverage_statements,
        transactions=coverage_transactions,
        expected_months=set(month_keys),
    )

    return AnalyticsResponse(
        period=AnalyticsPeriod(
            months=months,
            start_month=month_keys[0],
            end_month=month_keys[-1],
            currency="CNY",
            generated_on=today,
        ),
        coverage=AnalyticsCoverage(
            status=coverage.status.value,
            complete_statement_count=coverage.complete_statement_count,
            partial_statement_count=coverage.partial_statement_count,
            unknown_transaction_count=coverage.unknown_transaction_count,
            undated_transaction_count=coverage.undated_transaction_count,
            latest_statement_date=coverage.latest_statement_date,
        ),
        monthly_consumption=[
            MonthlyConsumption(
                month=key,
                amount_minor=month_metrics[key].period_net_consumption_minor,
                transaction_count=month_metrics[key].purchase_count,
                gross_purchase_minor=month_metrics[key].gross_purchase_minor,
                refund_minor=month_metrics[key].refund_observed_minor,
                net_consumption_minor=month_metrics[key].period_net_consumption_minor,
                credit_card_amount_minor=month_metrics[
                    key
                ].period_net_consumption_minor,
                manual_bill_amount_minor=int(manual_consumption[key]["amount_minor"]),
                manual_bill_count=int(manual_consumption[key]["count"]),
                total_amount_minor=(
                    month_metrics[key].period_net_consumption_minor
                    + int(manual_consumption[key]["amount_minor"])
                ),
                bank_usage=[
                    _monthly_bank_usage(
                        bank_code=bank_code,
                        bank_name=bank_name,
                        amount_minor=bank_month_metrics[key][
                            bank_key
                        ].period_net_consumption_minor,
                        credit_limit_minor=bank_limits[bank_key],
                    )
                    for bank_key in sorted(bank_limits, key=lambda item: item[1])
                    for bank_code, bank_name in [bank_key]
                ],
            )
            for key in month_keys
        ],
        monthly_due=[
            MonthlyDue(
                month=key,
                credit_card_remaining_minor=statement_due[key],
                manual_bill_remaining_minor=manual_due[key],
                total_remaining_minor=statement_due[key] + manual_due[key],
            )
            for key in month_keys
        ],
        bank_utilization=bank_utilization,
        card_utilization=card_utilization,
        repayment_trend=[
            MonthlyRepayment(
                month=key,
                credit_card_paid_minor=repayments[key]["credit_card_paid_minor"],
                manual_bill_paid_minor=repayments[key]["manual_bill_paid_minor"],
                total_paid_minor=(
                    repayments[key]["credit_card_paid_minor"]
                    + repayments[key]["manual_bill_paid_minor"]
                ),
            )
            for key in month_keys
        ],
    )
