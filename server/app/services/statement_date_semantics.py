"""Narrow adapter between physical statement records and account rule anchors.

Most issuers expose a statement date that is also the account's configured
statement-day occurrence. ICBC's verified natural-month email template is the
exception: it records the cycle end / generation date at month end, while the
account statement day is 1 and the logical rule anchor is the following day.

Keep that exception here so imports, history analysis, and backtests do not
independently infer ICBC semantics from a bare month-end date.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from app.parsers.base import ParsedStatement


def _is_month_end(value: date) -> bool:
    return value.day == calendar.monthrange(value.year, value.month)[1]


def is_verified_icbc_natural_month(parsed: ParsedStatement) -> bool:
    """Return whether parsed cycle facts prove the verified ICBC natural-month template."""
    start = parsed.billing_cycle_start
    end = parsed.billing_cycle_end
    return (
        parsed.bank_code == "ICBC"
        and start is not None
        and end is not None
        and start.day == 1
        and start.year == end.year
        and start.month == end.month
        and _is_month_end(end)
        and parsed.statement_date == end
    )


def parsed_account_statement_day(parsed: ParsedStatement) -> int | None:
    """Return an account statement day only when parser evidence proves it."""
    if is_verified_icbc_natural_month(parsed):
        return 1
    return None


def parsed_statement_day_boundary(parsed: ParsedStatement) -> str | None:
    """Return the account's same-day cycle boundary when cycle evidence proves it."""
    if is_verified_icbc_natural_month(parsed):
        return "next_cycle"
    return None


def logical_rule_anchor(
    physical_statement_date: date,
    *,
    bank_code: str | None,
    statement_day: int | None,
) -> date:
    """Map a stored physical statement date to the anchor used by due-rule math.

    Historical ICBC rows do not persist billing-cycle metadata. Normalization is
    therefore intentionally gated by both frozen account facts (day 1) and a
    physical month-end record. Old 2017 day-11 rows remain unchanged and are
    excluded from the current regime.
    """
    if bank_code == "ICBC" and statement_day == 1 and _is_month_end(physical_statement_date):
        return physical_statement_date + timedelta(days=1)
    return physical_statement_date


def preferred_equivalent_rule_type(
    *, bank_code: str | None, statement_day: int | None
) -> str | None:
    """Preserve account semantics when equivalent historical rules survive."""
    if bank_code == "ICBC" and statement_day == 1:
        return "fixed_day"
    return None
