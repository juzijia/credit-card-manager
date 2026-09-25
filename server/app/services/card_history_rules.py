"""Historical statement-based due-date rule analysis.

Replays historical statement_date / due_date pairs against the complete
production candidate space to infer deterministic due-date rules:
- fixed_day = N (1 <= N <= 31)
- days_after_statement = N (1 <= N <= 100)

Classification is mathematical diagnostics; the consumable outcome is
`analysis.rule`, which is set whenever the survivors' FUTURE due-date
behaviour is determined:
- UNIQUE (single survivor) -> rule = the survivor.
- AMBIGUOUS_EQUIVALENT with multiple survivors whose predicted due dates are
  identical across a rolling future calendar window (leap Februaries, 30/31-day
  months) -> rule = a behavioural representative (days_after_statement preferred).
- AMBIGUOUS_EQUIVALENT whose survivors diverge on some future month, CONFLICTING,
  or INSUFFICIENT -> rule is None (future behaviour not determined).

Architecture precedence:
explicit Card rule > card-history rule > product override > bank rule > unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.statement import Statement
from app.modules.optional.interest_free import (
    DueDateRuleType,
    clamped_statement_date,
    estimate_due_date,
)
from app.services.statement_date_semantics import (
    logical_rule_anchor,
    preferred_equivalent_rule_type,
)


class HistoryRuleClassification(StrEnum):
    UNIQUE = "UNIQUE"
    AMBIGUOUS_EQUIVALENT = "AMBIGUOUS_EQUIVALENT"
    CONFLICTING = "CONFLICTING"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True)
class CandidateRule:
    rule_type: str  # "fixed_day" | "days_after_statement"
    rule_value: int


@dataclass(frozen=True)
class HistoricalRuleAnalysis:
    classification: HistoryRuleClassification
    survivors: list[CandidateRule]
    applicable_pair_count: int
    rule: CandidateRule | None = None
    excluded_regime_pair_count: int = 0
    behavior_determined: bool = False
    target_statement_date: date | None = None
    last_verified_date: date | None = None


# Rolling future window (months) used to decide whether ambiguous survivors
# produce identical due dates for every future cycle. 60 months always covers
# a leap February plus 30- and 31-day months for any start month.
_BEHAVIOR_WINDOW_MONTHS = 60


def is_current_regime_compatible(
    statement_date: date,
    statement_day: int,
    *,
    bank_code: str | None = None,
) -> bool:
    """Return whether a physical row belongs to the current account regime."""
    if not 1 <= statement_day <= 31:
        return False
    try:
        rule_anchor = logical_rule_anchor(
            statement_date,
            bank_code=bank_code,
            statement_day=statement_day,
        )
        return rule_anchor == clamped_statement_date(rule_anchor, statement_day)
    except (ValueError, TypeError):
        return False


def _generate_candidate_rules() -> tuple[CandidateRule, ...]:
    candidates: list[CandidateRule] = []
    for day in range(1, 32):
        candidates.append(CandidateRule(rule_type=DueDateRuleType.FIXED_DAY.value, rule_value=day))
    for days in range(1, 101):
        candidates.append(CandidateRule(rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT.value, rule_value=days))
    return tuple(candidates)


_CANDIDATE_RULES: tuple[CandidateRule, ...] = _generate_candidate_rules()


def _survivor_future_behavior_is_determined(
    survivors: list[CandidateRule],
    statement_day: int | None,
) -> bool:
    """True iff all survivors predict the same due date for every future cycle.

    Mathematically at most one fixed_day and one days_after_statement candidate
    can survive the same pair set, so only the two-survivor mixed case needs the
    behavioural check. The check compares predicted due dates over a rolling
    future window of clamped statement dates; any divergence (typically caused
    by leap Februaries or 30- vs 31-day months) means future behaviour is not
    determined.
    """
    if statement_day is None or not 1 <= statement_day <= 31:
        return False
    if len(survivors) != 2:
        return False
    types = {s.rule_type for s in survivors}
    if types != {"fixed_day", "days_after_statement"}:
        return False

    anchor = date.today().replace(day=1)
    for offset in range(_BEHAVIOR_WINDOW_MONTHS):
        month = anchor + timedelta(days=offset * 31)
        # Normalize to the first of the month (offset*31 drifts across month ends).
        month = month.replace(day=1)
        try:
            stmt_date = clamped_statement_date(month, statement_day)
        except (ValueError, TypeError):
            return False
        predicted: set[date] = set()
        for survivor in survivors:
            try:
                predicted.add(
                    estimate_due_date(
                        statement_date=stmt_date,
                        rule_type=survivor.rule_type,
                        rule_value=survivor.rule_value,
                    )
                )
            except (ValueError, TypeError):
                return False
        if len(predicted) != 1:
            return False
    return True


def _behavior_representative(
    survivors: list[CandidateRule], *, preferred_rule_type: str | None = None
) -> CandidateRule:
    """Pick a semantically faithful representative among equivalent survivors."""
    if preferred_rule_type is not None:
        for survivor in survivors:
            if survivor.rule_type == preferred_rule_type:
                return survivor
    for survivor in survivors:
        if survivor.rule_type == "days_after_statement":
            return survivor
    return survivors[0]


def analyze_historical_due_date_rules(
    pairs: Iterable[tuple[date | None, date | None]],
    *,
    statement_day: int | None = None,
    target_statement_date: date | None = None,
    bank_code: str | None = None,
) -> HistoricalRuleAnalysis:
    """Analyze historical statement_date / due_date pairs against all supported candidates.

    Invariants:
    - Missing statement_date or due_date pairs are non-applicable (skipped).
    - Rows incompatible with current Card.statement_day regime are excluded from active candidate elimination.
    - Malformed pairs (e.g. due_date <= statement_date) or duplicate-date conflicting facts
      within the active regime are retained in applicable pairs so candidate elimination occurs deterministically.
    - Identical duplicates are harmless.
    - If applicable pair count < 2: classification is INSUFFICIENT.
    - If applicable pair count >= 2:
        survivors == 0 => CONFLICTING
        survivors == 1 => UNIQUE
        survivors > 1 => AMBIGUOUS_EQUIVALENT
    - `rule` (future-behaviour outcome) is set for UNIQUE, and for
      AMBIGUOUS_EQUIVALENT survivors that predict identical future due dates;
      otherwise it stays None.
    """
    applicable_pairs: list[tuple[date, date]] = []
    excluded_regime_count: int = 0
    for physical_date, d_date in pairs:
        if physical_date is None or d_date is None:
            continue
        if statement_day is not None and not is_current_regime_compatible(
            physical_date, statement_day, bank_code=bank_code
        ):
            excluded_regime_count += 1
            continue
        rule_anchor = logical_rule_anchor(
            physical_date,
            bank_code=bank_code,
            statement_day=statement_day,
        )
        applicable_pairs.append((rule_anchor, d_date))

    preferred_rule_type = preferred_equivalent_rule_type(
        bank_code=bank_code,
        statement_day=statement_day,
    )

    last_verified_date = max((s_date for s_date, _ in applicable_pairs), default=None)

    if len(applicable_pairs) < 2:
        return HistoricalRuleAnalysis(
            classification=HistoryRuleClassification.INSUFFICIENT,
            survivors=[],
            applicable_pair_count=len(applicable_pairs),
            rule=None,
            excluded_regime_pair_count=excluded_regime_count,
        )

    survivors: list[CandidateRule] = []
    for candidate in _CANDIDATE_RULES:
        matches_all = True
        for s_date, d_date in applicable_pairs:
            try:
                predicted_due = estimate_due_date(
                    statement_date=s_date,
                    rule_type=candidate.rule_type,
                    rule_value=candidate.rule_value,
                )
            except (ValueError, TypeError):
                matches_all = False
                break

            if predicted_due != d_date:
                matches_all = False
                break

        if matches_all:
            survivors.append(candidate)

    if not survivors:
        return HistoricalRuleAnalysis(
            classification=HistoryRuleClassification.CONFLICTING,
            survivors=[],
            applicable_pair_count=len(applicable_pairs),
            rule=None,
            excluded_regime_pair_count=excluded_regime_count,
        )

    if len(survivors) == 1:
        return HistoricalRuleAnalysis(
            classification=HistoryRuleClassification.UNIQUE,
            survivors=survivors,
            applicable_pair_count=len(applicable_pairs),
            rule=survivors[0],
            excluded_regime_pair_count=excluded_regime_count,
            behavior_determined=True,
            target_statement_date=target_statement_date,
            last_verified_date=last_verified_date,
        )

    # When a target_statement_date is provided (P0-C):
    # If all surviving rules predict the exact same due date for target_statement_date,
    # the target cycle's due date is fully determined (RESOLVED) for this spend.
    if target_statement_date is not None:
        predicted_dues: set[date] = set()
        valid = True
        for survivor in survivors:
            try:
                predicted_dues.add(
                    estimate_due_date(
                        statement_date=target_statement_date,
                        rule_type=survivor.rule_type,
                        rule_value=survivor.rule_value,
                    )
                )
            except (ValueError, TypeError):
                valid = False
                break
        if valid and len(predicted_dues) == 1:
            return HistoricalRuleAnalysis(
                classification=HistoryRuleClassification.AMBIGUOUS_EQUIVALENT,
                survivors=survivors,
                applicable_pair_count=len(applicable_pairs),
                rule=_behavior_representative(
                    survivors, preferred_rule_type=preferred_rule_type
                ),
                excluded_regime_pair_count=excluded_regime_count,
                behavior_determined=True,
                target_statement_date=target_statement_date,
                last_verified_date=last_verified_date,
            )
        # Truly divergent on target_statement_date (e.g. 10/25 vs 10/27) -> unresolved
        return HistoricalRuleAnalysis(
            classification=HistoryRuleClassification.AMBIGUOUS_EQUIVALENT,
            survivors=survivors,
            applicable_pair_count=len(applicable_pairs),
            rule=None,
            excluded_regime_pair_count=excluded_regime_count,
            behavior_determined=False,
            target_statement_date=target_statement_date,
        )

    # Fallback when no target_statement_date is given (generic analysis): 60-month window
    if _survivor_future_behavior_is_determined(survivors, statement_day):
        return HistoricalRuleAnalysis(
            classification=HistoryRuleClassification.AMBIGUOUS_EQUIVALENT,
            survivors=survivors,
            applicable_pair_count=len(applicable_pairs),
            rule=_behavior_representative(
                survivors, preferred_rule_type=preferred_rule_type
            ),
            excluded_regime_pair_count=excluded_regime_count,
            behavior_determined=True,
            last_verified_date=last_verified_date,
        )

    return HistoricalRuleAnalysis(
        classification=HistoryRuleClassification.AMBIGUOUS_EQUIVALENT,
        survivors=survivors,
        applicable_pair_count=len(applicable_pairs),
        rule=None,
        excluded_regime_pair_count=excluded_regime_count,
    )


def resolve_card_history_due_date_rule(
    session: Session,
    card_id: str,
    statement_day: int | None = None,
    target_statement_date: date | None = None,
    history_pairs: list[tuple[date | None, date | None]] | None = None,
    bank_code: str | None = None,
) -> HistoricalRuleAnalysis:
    """Query Statement rows for card_id and perform historical rule analysis for the current regime.

    Runtime query; executes purely read-only statements and does not mutate persistent business state.
    If statement_day is not passed, retrieves it from the Card model in session.
    If history_pairs is provided, skips the session query (used for walk-forward temporal backtests).
    """
    if statement_day is None or bank_code is None:
        from app.models.card import Card
        card = session.get(Card, card_id)
        if card is not None:
            if statement_day is None:
                statement_day = card.statement_day
            if bank_code is None:
                bank_code = card.bank_code

    if history_pairs is None:
        if bank_code == "CCB":
            stmt_query = (
                select(Statement)
                .where(Statement.bank_code == "CCB", Statement.card_id.is_(None))
                .order_by(Statement.statement_date.asc())
            )
        else:
            stmt_query = (
                select(Statement)
                .where(Statement.card_id == card_id)
                .order_by(Statement.statement_date.asc())
            )
        stmts = session.scalars(stmt_query).all()
        pairs = [(stmt.statement_date, stmt.due_date) for stmt in stmts]
    else:
        pairs = history_pairs

    return analyze_historical_due_date_rules(
        pairs,
        statement_day=statement_day,
        target_statement_date=target_statement_date,
        bank_code=bank_code,
    )
