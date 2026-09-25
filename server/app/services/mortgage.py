import math
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.manual_bill import ManualBill, ManualBillPayment
from app.models.mortgage import MortgagePlan
from app.models.mixins import utc_now


def round2(val: float) -> float:
    return round(val + 1e-12, 2)


def add_months(d: date, months: int) -> date:
    """Add n calendar months to date d, clamping day to end of month if necessary."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    max_day = monthrange(y, m)[1]
    return date(y, m, min(d.day, max_day))


@dataclass(frozen=True, slots=True)
class MortgageScheduleItem:
    period: int
    due_date: date
    payment_minor: int
    principal_minor: int
    interest_minor: int
    remaining_principal_minor: int


def calculate_equal_payment_schedule(
    principal_minor: int,
    annual_rate: float,
    months: int,
    start_date: date,
    start_period: int = 1,
) -> list[MortgageScheduleItem]:
    P = principal_minor / 100.0
    r = (annual_rate / 100.0) / 12.0
    if r == 0:
        monthly_payment = P / months
    else:
        factor = math.pow(1.0 + r, months)
        monthly_payment = P * r * factor / (factor - 1.0)

    schedule: list[MortgageScheduleItem] = []
    remaining = P
    for i in range(1, months + 1):
        due = add_months(start_date, i - start_period)
        if i < months:
            interest = round2(remaining * r)
            principal = round2(monthly_payment - interest)
            cur_payment = round2(principal + interest)
            remaining = round2(remaining - principal)
        else:
            principal = round2(remaining)
            interest = round2(remaining * r)
            cur_payment = round2(principal + interest)
            remaining = 0.0

        schedule.append(
            MortgageScheduleItem(
                period=i,
                due_date=due,
                payment_minor=int(round(cur_payment * 100)),
                principal_minor=int(round(principal * 100)),
                interest_minor=int(round(interest * 100)),
                remaining_principal_minor=int(round(remaining * 100)),
            )
        )
    return schedule


def calculate_equal_principal_schedule(
    principal_minor: int,
    annual_rate: float,
    months: int,
    start_date: date,
    start_period: int = 1,
) -> list[MortgageScheduleItem]:
    P = principal_minor / 100.0
    r = (annual_rate / 100.0) / 12.0
    base_p = P / months

    schedule: list[MortgageScheduleItem] = []
    remaining = P
    for i in range(1, months + 1):
        due = add_months(start_date, i - start_period)
        if i < months:
            principal = round2(base_p)
            interest = round2(remaining * r)
            cur_payment = round2(principal + interest)
            remaining = round2(remaining - principal)
        else:
            principal = round2(remaining)
            interest = round2(remaining * r)
            cur_payment = round2(principal + interest)
            remaining = 0.0

        schedule.append(
            MortgageScheduleItem(
                period=i,
                due_date=due,
                payment_minor=int(round(cur_payment * 100)),
                principal_minor=int(round(principal * 100)),
                interest_minor=int(round(interest * 100)),
                remaining_principal_minor=int(round(remaining * 100)),
            )
        )
    return schedule


def generate_plan_schedule(plan: MortgagePlan) -> list[MortgageScheduleItem]:
    """Generates the full schedule dynamically for a given MortgagePlan."""
    months = plan.loan_term_months
    start_date = plan.first_payment_date
    start_period = getattr(plan, "start_period", 1) or 1

    if plan.loan_type == "commercial":
        rate = plan.commercial_actual_rate or 0.0
        if plan.repayment_method == "equal_principal":
            return calculate_equal_principal_schedule(plan.commercial_principal_minor, rate, months, start_date, start_period)
        return calculate_equal_payment_schedule(plan.commercial_principal_minor, rate, months, start_date, start_period)

    elif plan.loan_type == "provident_fund":
        rate = plan.fund_rate or 0.0
        if plan.repayment_method == "equal_principal":
            return calculate_equal_principal_schedule(plan.fund_principal_minor, rate, months, start_date, start_period)
        return calculate_equal_payment_schedule(plan.fund_principal_minor, rate, months, start_date, start_period)

    elif plan.loan_type == "combined":
        c_rate = plan.commercial_actual_rate or 0.0
        f_rate = plan.fund_rate or 0.0

        if plan.repayment_method == "equal_principal":
            c_sched = calculate_equal_principal_schedule(plan.commercial_principal_minor, c_rate, months, start_date, start_period)
            f_sched = calculate_equal_principal_schedule(plan.fund_principal_minor, f_rate, months, start_date, start_period)
        else:
            c_sched = calculate_equal_payment_schedule(plan.commercial_principal_minor, c_rate, months, start_date, start_period)
            f_sched = calculate_equal_payment_schedule(plan.fund_principal_minor, f_rate, months, start_date, start_period)

        merged: list[MortgageScheduleItem] = []
        for i in range(months):
            c_item = c_sched[i]
            f_item = f_sched[i]
            merged.append(
                MortgageScheduleItem(
                    period=i + 1,
                    due_date=c_item.due_date,
                    payment_minor=c_item.payment_minor + f_item.payment_minor,
                    principal_minor=c_item.principal_minor + f_item.principal_minor,
                    interest_minor=c_item.interest_minor + f_item.interest_minor,
                    remaining_principal_minor=c_item.remaining_principal_minor + f_item.remaining_principal_minor,
                )
            )
        return merged

    return []


def get_plan_period_for_date(plan: MortgagePlan, target_date: date) -> MortgageScheduleItem | None:
    """Find the schedule item whose month matches target_date's month."""
    schedule = generate_plan_schedule(plan)
    target_period = target_date.strftime("%Y-%m")
    for item in schedule:
        if item.due_date.strftime("%Y-%m") == target_period:
            return item
    return None

