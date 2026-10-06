from datetime import date, timedelta

import pytest

from app.modules.optional.interest_free import (
    DueDateRuleType,
    EstimationLevel,
    StatementBoundaryState,
    calculate_interest_free_days,
    calculate_theoretical_max_interest_free_days,
    forecast_interest_free_days,
)


def test_actual_statement_dates_are_authoritative_from_transaction_date() -> None:
    # Counter-example 1: transaction=9/10, posting=9/11, due=9/30 -> strictly 20 days, not 19
    result = calculate_interest_free_days(
        card_id="card-1",
        transaction_date=date(2026, 9, 10),
        posting_date=date(2026, 9, 11),
        due_date=date(2026, 9, 30),
        statement_date=date(2026, 9, 15),
    )
    assert result.interest_free_days == 20
    assert result.estimation_level is EstimationLevel.ACTUAL
    assert result.posting_date == date(2026, 9, 11)
    assert result.statement_date == date(2026, 9, 15)


def test_missing_posting_date_remains_actual() -> None:
    result = calculate_interest_free_days(
        card_id="card-1",
        transaction_date=date(2026, 9, 1),
        posting_date=None,
        due_date=date(2026, 9, 27),
    )
    assert result.interest_free_days == 26
    assert result.estimation_level is EstimationLevel.ACTUAL
    assert result.posting_date is None


def test_due_date_before_transaction_date_raises() -> None:
    with pytest.raises(ValueError, match="due_date must not be earlier than transaction_date"):
        calculate_interest_free_days(
            card_id="card-1",
            transaction_date=date(2026, 9, 28),
            posting_date=date(2026, 9, 28),
            due_date=date(2026, 9, 27),
        )


def test_exact_statement_day_with_next_cycle_selects_next_cycle() -> None:
    result = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 9, 9),
        statement_day=9,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=27,
        boundary_state=StatementBoundaryState.NEXT_CYCLE,
    )
    assert result.boundary_state is StatementBoundaryState.NEXT_CYCLE
    assert result.selected_statement_date == date(2026, 10, 9)
    assert result.selected_due_date == date(2026, 10, 27)
    assert result.conservative_interest_free_days == (date(2026, 10, 27) - date(2026, 9, 9)).days
    assert result.conservative is True


def test_exact_statement_day_with_uncertain_boundary_is_conservative() -> None:
    result = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 9, 5),
        statement_day=5,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=25,
        boundary_state=StatementBoundaryState.UNCERTAIN,
    )
    assert result.boundary_state is StatementBoundaryState.UNCERTAIN
    assert result.selected_statement_date == date(2026, 9, 5)
    assert result.selected_due_date == date(2026, 9, 25)
    assert result.conservative_interest_free_days == 20
    assert result.conservative is True


def test_exact_statement_day_with_current_cycle_selects_current_cycle() -> None:
    result = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 8, 23),
        statement_day=23,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=25,
        boundary_state=StatementBoundaryState.CURRENT_CYCLE,
    )
    assert result.boundary_state is StatementBoundaryState.CURRENT_CYCLE
    assert result.selected_statement_date == date(2026, 8, 23)
    assert result.selected_due_date == date(2026, 9, 17)
    assert result.conservative_interest_free_days == 25


def test_bank_code_does_not_affect_forecast_result() -> None:
    result = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 9, 9),
        statement_day=9,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=18,
        bank_code="ABC",
        boundary_state=StatementBoundaryState.UNCERTAIN,
    )
    res_other = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 9, 9),
        statement_day=9,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=18,
        bank_code="UNKNOWN_BANK",
        boundary_state=StatementBoundaryState.UNCERTAIN,
    )
    res_none = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 9, 9),
        statement_day=9,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=18,
        bank_code=None,
        boundary_state=StatementBoundaryState.UNCERTAIN,
    )
    assert result == res_other == res_none
    assert result.boundary_state is StatementBoundaryState.UNCERTAIN
    assert result.selected_statement_date == date(2026, 9, 9)
    assert result.selected_due_date == date(2026, 9, 27)
    assert result.conservative_interest_free_days == 18


def test_counter_example_6_spend_date_before_statement_date() -> None:
    # Counter-example 6: spend_date = statement_date - 1 day -> conservative earliest due is current cycle
    result = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 9, 8),
        statement_day=9,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=27,
        boundary_state=StatementBoundaryState.UNCERTAIN,
    )
    assert result.selected_statement_date == date(2026, 9, 9)
    assert result.selected_due_date == date(2026, 9, 27)
    assert result.conservative_interest_free_days == 19


def test_counter_example_7_spend_date_after_statement_date() -> None:
    # Counter-example 7: spend_date = statement_date + 1 day -> next cycle
    result = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 9, 10),
        statement_day=9,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=27,
        boundary_state=StatementBoundaryState.UNCERTAIN,
    )
    assert result.selected_statement_date == date(2026, 10, 9)
    assert result.selected_due_date == date(2026, 10, 27)
    assert result.conservative_interest_free_days == (date(2026, 10, 27) - date(2026, 9, 10)).days


def test_counter_example_8_statement_day_31_february_month_end() -> None:
    # Non-leap year February (2026) clamps 31 to 28
    res_non_leap = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 2, 28),
        statement_day=31,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=25,
        boundary_state=StatementBoundaryState.UNCERTAIN,
    )
    assert res_non_leap.selected_statement_date == date(2026, 2, 28)
    assert res_non_leap.selected_due_date == date(2026, 3, 25)
    assert res_non_leap.conservative_interest_free_days == 25

    # Leap year February (2028) clamps 31 to 29
    res_leap = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2028, 2, 29),
        statement_day=31,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=25,
        boundary_state=StatementBoundaryState.UNCERTAIN,
    )
    assert res_leap.selected_statement_date == date(2028, 2, 29)
    assert res_leap.selected_due_date == date(2028, 3, 25)
    assert res_leap.conservative_interest_free_days == 25


def test_counter_example_9_fixed_day_due_crosses_month() -> None:
    # Counter-example 9: statement day 17, due day 6 -> due date is next month 6th
    result = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 9, 17),
        statement_day=17,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=6,
        boundary_state=StatementBoundaryState.UNCERTAIN,
    )
    assert result.selected_statement_date == date(2026, 9, 17)
    assert result.selected_due_date == date(2026, 10, 6)
    assert result.conservative_interest_free_days == 19


@pytest.mark.parametrize(
    ("statement_date", "fixed_day", "expected_due_date"),
    [
        (date(2026, 2, 28), 30, date(2026, 3, 30)),
        (date(2028, 2, 29), 30, date(2028, 3, 30)),
        (date(2026, 4, 30), 31, date(2026, 5, 31)),
        (date(2026, 1, 30), 31, date(2026, 1, 31)),
        (date(2026, 1, 31), 31, date(2026, 2, 28)),
        (date(2026, 9, 17), 6, date(2026, 10, 6)),
    ],
)
def test_fixed_day_due_uses_actual_clamped_candidate_date(
    statement_date: date,
    fixed_day: int,
    expected_due_date: date,
) -> None:
    result = forecast_interest_free_days(
        card_id="card-1",
        spend_date=statement_date,
        statement_day=statement_date.day,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=fixed_day,
        boundary_state=StatementBoundaryState.CURRENT_CYCLE,
    )

    assert result.selected_statement_date == statement_date
    assert result.selected_due_date == expected_due_date


def test_counter_example_10_days_after_statement_crosses_february_and_leap() -> None:
    # Counter-example 10: days_after_statement crosses Feb
    # Non-leap year: 2026-02-23 + 25 days -> 2026-03-20
    res_non_leap = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 2, 23),
        statement_day=23,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=25,
        boundary_state=StatementBoundaryState.CURRENT_CYCLE,
    )
    assert res_non_leap.selected_due_date == date(2026, 3, 20)
    assert res_non_leap.conservative_interest_free_days == 25

    # Leap year: 2028-02-23 + 25 days -> 2028-03-19
    res_leap = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2028, 2, 23),
        statement_day=23,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=25,
        boundary_state=StatementBoundaryState.CURRENT_CYCLE,
    )
    assert res_leap.selected_due_date == date(2028, 3, 19)
    assert res_leap.conservative_interest_free_days == 25


def test_theoretical_max_interest_free_days_calculation() -> None:
    # CMB next_cycle statement_day 9, fixed_day 27 -> 49 days
    max_cmb = calculate_theoretical_max_interest_free_days(
        statement_day=9,
        boundary_state=StatementBoundaryState.NEXT_CYCLE,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=27,
    )
    assert max_cmb == 49

    # Uncertain boundary starts calculation the day after statement day
    max_boc = calculate_theoretical_max_interest_free_days(
        statement_day=25,
        boundary_state=StatementBoundaryState.UNCERTAIN,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=20,
    )
    assert max_boc == 50

    # ABC current_cycle statement_day 23, offset 25 -> 55 days
    max_abc = calculate_theoretical_max_interest_free_days(
        statement_day=23,
        boundary_state=StatementBoundaryState.CURRENT_CYCLE,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=25,
    )
    assert max_abc == 55

    # Invalid rule returns None
    assert calculate_theoretical_max_interest_free_days(
        statement_day=9,
        boundary_state=StatementBoundaryState.CURRENT_CYCLE,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=None,
    ) is None


def test_observed_only_cannot_enter_future_forecast() -> None:
    with pytest.raises(ValueError, match="observed_only is not a forecastable due date rule"):
        forecast_interest_free_days(
            card_id="test-card",
            spend_date=date(2026, 9, 9),
            statement_day=9,
            due_date_rule_type=DueDateRuleType.OBSERVED_ONLY,
            due_date_rule_value=None,
            boundary_state=StatementBoundaryState.UNCERTAIN,
        )


def test_forecast_is_deterministic_for_same_inputs() -> None:
    """Forecast depends only on (statement_day, rule, boundary); repeated
    calls with identical inputs return identical results."""
    res_a = forecast_interest_free_days(
        card_id="c1",
        spend_date=date(2026, 9, 1),
        statement_day=9,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=27,
    )
    res_b = forecast_interest_free_days(
        card_id="c1",
        spend_date=date(2026, 9, 1),
        statement_day=9,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=27,
    )
    assert res_a == res_b

    res_c = forecast_interest_free_days(
        card_id="c1",
        spend_date=date(2026, 9, 1),
        statement_day=9,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=20,
    )
    res_d = forecast_interest_free_days(
        card_id="c1",
        spend_date=date(2026, 9, 1),
        statement_day=9,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=20,
    )
    assert res_c == res_d


@pytest.mark.parametrize(
    ("spend_date", "expected_statement", "expected_due", "expected_days"),
    [
        (date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 25), 25),
        (date(2026, 9, 1), date(2026, 10, 1), date(2026, 10, 25), 54),
        (date(2026, 9, 2), date(2026, 10, 1), date(2026, 10, 25), 53),
    ],
)
def test_day1_account_uses_generic_next_cycle_boundary(
    spend_date: date,
    expected_statement: date,
    expected_due: date,
    expected_days: int,
) -> None:
    result = forecast_interest_free_days(
        card_id="icbc-account-fact",
        spend_date=spend_date,
        statement_day=1,
        due_date_rule_type=DueDateRuleType.FIXED_DAY,
        due_date_rule_value=25,
        boundary_state=StatementBoundaryState.NEXT_CYCLE,
    )

    assert result.selected_statement_date == expected_statement
    assert result.selected_due_date == expected_due
    assert result.conservative_interest_free_days == expected_days
