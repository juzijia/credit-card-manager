from calendar import monthrange
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.manual_bill import ManualBill, ManualBillPayment
from app.models.mortgage import MortgagePlan
from app.models.mixins import utc_now
from app.services.mortgage import get_plan_period_for_date


def current_period(today: date) -> str:
    return today.strftime("%Y-%m")


def _month_day(today: date, day: int) -> date:
    return date(today.year, today.month, min(day, monthrange(today.year, today.month)[1]))


def ensure_current_payments(session: Session, today: date) -> list[tuple[ManualBill, ManualBillPayment]]:
    bills = session.scalars(select(ManualBill).where(ManualBill.enabled.is_(True))).all()
    period = current_period(today)
    results: list[tuple[ManualBill, ManualBillPayment]] = []
    for bill in bills:
        # Plan-driven mortgage bills dynamically get amount & due_date from MortgagePlan
        amount_minor = bill.monthly_amount_minor
        due_date = _month_day(today, bill.due_day)

        if bill.source_type == "mortgage_plan" and bill.source_id:
            plan = session.scalar(select(MortgagePlan).where(MortgagePlan.id == bill.source_id))
            if plan and plan.enabled:
                sched_item = get_plan_period_for_date(plan, today)
                if sched_item:
                    amount_minor = sched_item.payment_minor
                    due_date = sched_item.due_date
                else:
                    # Current month is outside mortgage active schedule range (before first payment or after last period)
                    continue

        payment = session.scalar(
            select(ManualBillPayment).where(
                ManualBillPayment.manual_bill_id == bill.id,
                ManualBillPayment.period == period,
            )
        )
        if payment is None:
            payment = ManualBillPayment(
                manual_bill_id=bill.id,
                period=period,
                due_date=due_date,
                amount_minor=amount_minor,
            )
            session.add(payment)
            session.flush()
        results.append((bill, payment))
    return results


def list_current_payments(session: Session, today: date, period: str | None = None) -> list[tuple[ManualBill, ManualBillPayment]]:
    """Read existing payments for target period without creating or flushing rows."""
    target_period = period or current_period(today)
    rows = session.execute(
        select(ManualBill, ManualBillPayment)
        .join(ManualBillPayment, ManualBillPayment.manual_bill_id == ManualBill.id)
        .where(
            ManualBill.enabled.is_(True),
            ManualBillPayment.period == target_period,
        )
    ).all()
    return [(bill, payment) for bill, payment in rows]


def mark_paid(payment: ManualBillPayment, repayment_channel: str | None = None) -> None:
    payment.paid_at = utc_now()
    payment.repayment_channel = repayment_channel


def unmark_paid(payment: ManualBillPayment) -> None:
    payment.paid_at = None
    payment.repayment_channel = None
