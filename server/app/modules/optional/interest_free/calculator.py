"""Interest-free calculations using statement facts or an explicit card rule."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum


class DueDateRuleType(StrEnum):
    DAYS_AFTER_STATEMENT = "days_after_statement"
    FIXED_DAY = "fixed_day"
    OBSERVED_ONLY = "observed_only"


class EstimationLevel(StrEnum):
    ACTUAL = "actual"
    POSTING_DATE_ESTIMATED = "posting_date_estimated"
    DUE_DATE_ESTIMATED = "due_date_estimated"
    FULLY_ESTIMATED = "fully_estimated"


class StatementBoundaryState(StrEnum):
    CURRENT_CYCLE = "current_cycle"
    NEXT_CYCLE = "next_cycle"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class HistoricalInterestFreeResult:
    card_id: str
    spend_date: date
    statement_date: date | None
    due_date: date
    interest_free_days: int
    posting_date: date | None = None
    estimation_level: EstimationLevel = EstimationLevel.ACTUAL


@dataclass(frozen=True)
class FutureInterestFreeResult:
    card_id: str
    spend_date: date
    selected_statement_date: date
    selected_due_date: date
    conservative_interest_free_days: int
    theoretical_max_interest_free_days: int | None
    boundary_state: StatementBoundaryState
    conservative: bool = True
    estimation_level: EstimationLevel = EstimationLevel.FULLY_ESTIMATED
    rule_source: str = "explicit_card"


# Backwards compatibility alias if needed
InterestFreeEstimate = FutureInterestFreeResult


def _month_date(anchor: date, months: int, day: int) -> date:
    month_index = anchor.year * 12 + anchor.month - 1 + months
    year, month_zero_based = divmod(month_index, 12)
    month = month_zero_based + 1
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def calculate_theoretical_max_interest_free_days(
    *,
    statement_day: int,
    boundary_state: StatementBoundaryState | str,
    due_date_rule_type: DueDateRuleType | str,
    due_date_rule_value: int | None = None,
) -> int | None:
    """Calculate the maximum achievable interest-free days in the current regime across calendar cycles.

    For next_cycle banks, spend on statement day itself achieves the longest cycle.
    For other boundary states, the day after statement day is the stable start into the next cycle.
    """
    try:
        b_state = StatementBoundaryState(boundary_state)
        max_days = 0
        for month in range(1, 13):
            anchor = date(2026, month, 1)
            curr_stmt = _month_date(anchor, 0, statement_day)
            if b_state is StatementBoundaryState.NEXT_CYCLE:
                best_spend = curr_stmt
                target_stmt = _month_date(curr_stmt, 1, statement_day)
            else:
                best_spend = curr_stmt + timedelta(days=1)
                target_stmt = _month_date(curr_stmt, 1, statement_day)
            due = _estimated_due_date(
                target_stmt,
                due_date_rule_type,
                due_date_rule_value,
            )
            days = (due - best_spend).days
            if days > max_days:
                max_days = days
        return max_days if max_days > 0 else None
    except (ValueError, TypeError):
        return None


def _estimated_due_date(
    statement_date: date,
    rule_type: DueDateRuleType | str,
    rule_value: int | None,
) -> date:
    rule_type = DueDateRuleType(rule_type)
    if rule_type is DueDateRuleType.DAYS_AFTER_STATEMENT:
        if rule_value is None or not 1 <= rule_value <= 100:
            raise ValueError("days_after_statement requires a value between 1 and 100")
        return statement_date + timedelta(days=rule_value)
    if rule_type is DueDateRuleType.FIXED_DAY:
        if rule_value is None or not 1 <= rule_value <= 31:
            raise ValueError("fixed_day requires a value between 1 and 31")
        same_month = _month_date(statement_date, 0, rule_value)
        return same_month if same_month > statement_date else _month_date(statement_date, 1, rule_value)
    if rule_type is DueDateRuleType.OBSERVED_ONLY:
        raise ValueError("observed_only is not a forecastable due date rule")
    raise ValueError(f"unsupported due date rule type: {rule_type}")


def estimate_due_date(
    statement_date: date,
    rule_type: DueDateRuleType | str,
    rule_value: int | None,
) -> date:
    """Public seam for production due-date calculation semantics."""
    return _estimated_due_date(
        statement_date=statement_date,
        rule_type=rule_type,
        rule_value=rule_value,
    )


def clamped_statement_date(anchor: date, statement_day: int) -> date:
    """Return the canonical clamped occurrence of statement_day in anchor's calendar month."""
    if not 1 <= statement_day <= 31:
        raise ValueError("statement_day must be between 1 and 31")
    return _month_date(anchor, 0, statement_day)


def calculate_interest_free_days(
    *,
    card_id: str,
    transaction_date: date,
    posting_date: date | None = None,
    due_date: date,
    statement_date: date | None = None,
) -> HistoricalInterestFreeResult:
    """Calculate historical user-perspective interest-free days from transaction date to statement due date.

    Actual interest-free days is strictly (due_date - transaction_date).
    posting_date is retained for audit/explanation only, never as the starting point.
    """
    if due_date < transaction_date:
        raise ValueError("due_date must not be earlier than transaction_date")
    return HistoricalInterestFreeResult(
        card_id=card_id,
        spend_date=transaction_date,
        statement_date=statement_date,
        due_date=due_date,
        interest_free_days=(due_date - transaction_date).days,
        posting_date=posting_date,
        estimation_level=EstimationLevel.ACTUAL,
    )


def target_statement_date(
    spend_date: date,
    statement_day: int,
    boundary_state: StatementBoundaryState | str = StatementBoundaryState.UNCERTAIN,
) -> date:
    """Determine the effective target statement date for a transaction spend date."""
    if not 1 <= statement_day <= 31:
        raise ValueError("statement_day must be between 1 and 31")
    resolved_boundary = StatementBoundaryState(boundary_state)
    current_stmt = _month_date(spend_date, 0, statement_day)
    if spend_date < current_stmt:
        return current_stmt
    elif spend_date > current_stmt:
        return _month_date(spend_date, 1, statement_day)
    else:  # spend_date == current_stmt
        if resolved_boundary is StatementBoundaryState.NEXT_CYCLE:
            return _month_date(spend_date, 1, statement_day)
        return current_stmt


def forecast_interest_free_days(
    *,
    card_id: str,
    spend_date: date,
    statement_day: int,
    due_date_rule_type: DueDateRuleType | str,
    due_date_rule_value: int | None = None,
    bank_code: str | None = None,
    boundary_state: StatementBoundaryState | str = StatementBoundaryState.UNCERTAIN,
    rule_source: str = "explicit_card",
) -> FutureInterestFreeResult:
    """Forecast conservative interest-free days using the card's regime and boundary state.

    Selects earliest plausible statement cycle:
    - spend_date < current_month_statement -> current cycle
    - spend_date > current_month_statement -> next cycle
    - spend_date == current_month_statement:
        next_cycle -> next cycle
        current_cycle / uncertain -> current cycle (conservative)
    """
    if not 1 <= statement_day <= 31:
        raise ValueError("statement_day must be between 1 and 31")

    resolved_boundary = StatementBoundaryState(boundary_state)

    selected_statement_date = target_statement_date(
        spend_date=spend_date,
        statement_day=statement_day,
        boundary_state=resolved_boundary,
    )

    selected_due_date = _estimated_due_date(
        selected_statement_date,
        due_date_rule_type,
        due_date_rule_value,
    )

    conservative_days = (selected_due_date - spend_date).days

    theoretical_max = calculate_theoretical_max_interest_free_days(
        statement_day=statement_day,
        boundary_state=resolved_boundary,
        due_date_rule_type=due_date_rule_type,
        due_date_rule_value=due_date_rule_value,
    )

    return FutureInterestFreeResult(
        card_id=card_id,
        spend_date=spend_date,
        selected_statement_date=selected_statement_date,
        selected_due_date=selected_due_date,
        conservative_interest_free_days=conservative_days,
        theoretical_max_interest_free_days=theoretical_max,
        boundary_state=resolved_boundary,
        conservative=True,
        estimation_level=EstimationLevel.FULLY_ESTIMATED,
        rule_source=rule_source,
    )
