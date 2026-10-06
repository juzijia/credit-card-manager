"""Tests for deterministic historical card due-date rule resolution with current-regime scoping."""

from datetime import date
import pytest
from sqlalchemy import select

from app.models.card import Card
from app.models.statement import Statement
from app.services.card_history_rules import (
    CandidateRule,
    HistoryRuleClassification,
    analyze_historical_due_date_rules,
    is_current_regime_compatible,
)
from app.modules.optional.interest_free import clamped_statement_date


# ---------------------------------------------------------------------------
# 1. Pure candidate & regime scoping tests
# ---------------------------------------------------------------------------


def test_pure_boc_unique_scenario():
    """BOC 8759 scenario: 3 statements across 30 and 31 day months.

    - 2021-04-25 (April 30 days) -> 2021-05-15 (+20 days, or fixed 15)
    - 2021-07-25 (July 31 days)  -> 2021-08-14 (+20 days, fixed 15 gives Aug 15 -> eliminated)
    - 2021-10-25 (Oct 31 days)   -> 2021-11-14 (+20 days, fixed 15 gives Nov 15 -> eliminated)
    Survivors: exactly days_after_statement=20 => UNIQUE.
    """
    pairs = [
        (date(2021, 4, 25), date(2021, 5, 15)),
        (date(2021, 7, 25), date(2021, 8, 14)),
        (date(2021, 10, 25), date(2021, 11, 14)),
    ]
    analysis = analyze_historical_due_date_rules(pairs, statement_day=25)
    assert analysis.classification == HistoryRuleClassification.UNIQUE
    assert analysis.applicable_pair_count == 3
    assert analysis.excluded_regime_pair_count == 0
    assert analysis.survivors == [CandidateRule("days_after_statement", 20)]
    assert analysis.rule == CandidateRule("days_after_statement", 20)


def test_pure_repeated_05_to_25_ambiguity():
    """CGB 1402/1721 scenario: statement day 5 -> due day 25 within the same month.

    Both fixed_day=25 and days_after_statement=20 match every pair, and their
    future due dates are identical for every month (5+20 always lands on the
    25th; a same-month fixed 25 can never cross). Behaviour is determined ->
    `rule` resolves to the days_after_statement representative.
    """
    pairs = [
        (date(2024, 6, 5), date(2024, 6, 25)),
        (date(2024, 11, 5), date(2024, 11, 25)),
        (date(2025, 1, 5), date(2025, 1, 25)),
        (date(2025, 2, 5), date(2025, 2, 25)),
        (date(2025, 3, 5), date(2025, 3, 25)),
    ]
    analysis = analyze_historical_due_date_rules(pairs, statement_day=5)
    assert analysis.classification == HistoryRuleClassification.AMBIGUOUS_EQUIVALENT
    assert analysis.applicable_pair_count == 5
    assert analysis.excluded_regime_pair_count == 0
    assert set(analysis.survivors) == {
        CandidateRule("fixed_day", 25),
        CandidateRule("days_after_statement", 20),
    }
    assert analysis.behavior_determined is True
    assert analysis.rule == CandidateRule("days_after_statement", 20)


def test_pure_repeated_05_to_24_ambiguity():
    """CITIC 7085/7539 scenario: statement day 5 -> due day 24 within the same month.

    Survivors {fixed_day=24, days_after_statement=19} are behaviourally
    equivalent (5+19 is always the same-month 24th) -> rule resolves.
    """
    pairs = [
        (date(2023, 7, 5), date(2023, 7, 24)),
        (date(2023, 8, 5), date(2023, 8, 24)),
        (date(2023, 12, 5), date(2023, 12, 24)),
        (date(2024, 3, 5), date(2024, 3, 24)),
    ]
    analysis = analyze_historical_due_date_rules(pairs, statement_day=5)
    assert analysis.classification == HistoryRuleClassification.AMBIGUOUS_EQUIVALENT
    assert analysis.applicable_pair_count == 4
    assert analysis.excluded_regime_pair_count == 0
    assert set(analysis.survivors) == {
        CandidateRule("fixed_day", 24),
        CandidateRule("days_after_statement", 19),
    }
    assert analysis.behavior_determined is True
    assert analysis.rule == CandidateRule("days_after_statement", 19)


def test_pure_day30_ambiguity_diverging_in_31_day_month_is_unresolved():
    """Statement day 30 observed only in 30-day months: fixed_day=20 vs
    days_after_statement=20 both survive the pairs, but they diverge on future
    31-day months (e.g. statement Jan 30 -> fixed 20 gives Feb 20, +20 gives
    Feb 19). Future behaviour is NOT determined -> rule stays None.
    """
    pairs = [
        (date(2024, 4, 30), date(2024, 5, 20)),
        (date(2024, 6, 30), date(2024, 7, 20)),
        (date(2024, 9, 30), date(2024, 10, 20)),
        (date(2024, 11, 30), date(2024, 12, 20)),
    ]
    analysis = analyze_historical_due_date_rules(pairs, statement_day=30)
    assert analysis.classification == HistoryRuleClassification.AMBIGUOUS_EQUIVALENT
    assert set(analysis.survivors) == {
        CandidateRule("fixed_day", 20),
        CandidateRule("days_after_statement", 20),
    }
    assert analysis.behavior_determined is False
    assert analysis.rule is None


def test_pure_day31_clamping_accepts_all_month_ends():
    """MANDATORY TEST 4: current day31 accepts Jan31, Feb28, Feb29, Apr30 as same current regime."""
    assert clamped_statement_date(date(2024, 1, 15), 31) == date(2024, 1, 31)
    assert clamped_statement_date(date(2022, 2, 10), 31) == date(2022, 2, 28)  # common year Feb 28
    assert clamped_statement_date(date(2024, 2, 10), 31) == date(2024, 2, 29)  # leap year Feb 29
    assert clamped_statement_date(date(2024, 4, 10), 31) == date(2024, 4, 30)  # 30-day month Apr 30

    assert is_current_regime_compatible(date(2024, 1, 31), 31) is True
    assert is_current_regime_compatible(date(2022, 2, 28), 31) is True
    assert is_current_regime_compatible(date(2024, 2, 29), 31) is True
    assert is_current_regime_compatible(date(2024, 4, 30), 31) is True
    assert is_current_regime_compatible(date(2017, 5, 11), 31) is False

    pairs = [
        (date(2024, 1, 31), date(2024, 2, 25)),
        (date(2022, 2, 28), date(2022, 3, 25)),
        (date(2024, 2, 29), date(2024, 3, 25)),
        (date(2024, 4, 30), date(2024, 5, 25)),
    ]
    analysis = analyze_historical_due_date_rules(pairs, statement_day=31)
    assert analysis.applicable_pair_count == 4
    assert analysis.excluded_regime_pair_count == 0
    assert analysis.classification == HistoryRuleClassification.AMBIGUOUS_EQUIVALENT
    assert set(analysis.survivors) == {
        CandidateRule("fixed_day", 25),
        CandidateRule("days_after_statement", 25),
    }
    # Day-31 statements always clamp to month end; +25 from month end always
    # lands on the next month's 25th, identical to fixed_day=25. Behaviour is
    # determined.
    assert analysis.behavior_determined is True
    assert analysis.rule == CandidateRule("days_after_statement", 25)


def test_pure_icbc9980_physical_dates_normalize_to_current_day1_regime():
    """ICBC physical month-end rows use next-day logical anchors for day-1 rules."""
    pairs = [
        # Older regime (day 11 statements)
        (date(2017, 5, 11), date(2017, 6, 5)),
        (date(2017, 6, 11), date(2017, 7, 5)),
        (date(2017, 10, 11), date(2017, 11, 5)),
        (date(2017, 11, 11), date(2017, 12, 5)),
        # Newer regime (month-end statements -> due 25th of next month)
        (date(2017, 12, 31), date(2018, 1, 25)),
        (date(2018, 1, 31), date(2018, 2, 25)),
        (date(2018, 2, 28), date(2018, 3, 25)),
        (date(2018, 3, 31), date(2018, 4, 25)),
        (date(2018, 4, 30), date(2018, 5, 25)),
    ]
    analysis = analyze_historical_due_date_rules(
        pairs, statement_day=1, bank_code="ICBC"
    )
    assert analysis.classification == HistoryRuleClassification.AMBIGUOUS_EQUIVALENT
    assert analysis.applicable_pair_count == 5  # the 5 month-end pairs
    assert analysis.excluded_regime_pair_count == 4  # the 4 day-11 pairs
    assert set(analysis.survivors) == {
        CandidateRule("fixed_day", 25),
        CandidateRule("days_after_statement", 24),
    }
    assert analysis.behavior_determined is True
    assert analysis.rule == CandidateRule("fixed_day", 25)

    # Diagnostics without statement_day regime evaluates globally and yields CONFLICTING
    global_analysis = analyze_historical_due_date_rules(pairs)
    assert global_analysis.classification == HistoryRuleClassification.CONFLICTING
    assert global_analysis.applicable_pair_count == 9
    assert global_analysis.survivors == []


def test_pure_stale_history_with_different_current_statement_day():
    """MANDATORY TEST 1 (pure logic): stale UNIQUE day25/+20 history + current statement_day10

    Zero compatible day10 rows => INSUFFICIENT.
    """
    pairs = [
        (date(2021, 4, 25), date(2021, 5, 15)),
        (date(2021, 7, 25), date(2021, 8, 14)),
        (date(2021, 10, 25), date(2021, 11, 14)),
    ]
    # Scoped to day 10
    analysis = analyze_historical_due_date_rules(pairs, statement_day=10)
    assert analysis.classification == HistoryRuleClassification.INSUFFICIENT
    assert analysis.applicable_pair_count == 0
    assert analysis.excluded_regime_pair_count == 3
    assert analysis.rule is None


def test_pure_active_regime_conflict():
    """Within-regime conflicting facts (e.g. same regime, but differing rules) produce CONFLICTING."""
    pairs = [
        (date(2024, 6, 25), date(2024, 7, 15)),  # +20 days or fixed 15
        (date(2024, 7, 25), date(2024, 8, 25)),  # +31 days or fixed 25 (conflicts with first)
    ]
    analysis = analyze_historical_due_date_rules(pairs, statement_day=25)
    assert analysis.classification == HistoryRuleClassification.CONFLICTING
    assert analysis.applicable_pair_count == 2
    assert analysis.survivors == []


def test_pure_impossible_no_candidate():
    """Impossible due dates where due_date < statement_date or beyond candidate range."""
    pairs_before = [
        (date(2024, 5, 15), date(2024, 5, 10)),
        (date(2024, 6, 15), date(2024, 6, 10)),
    ]
    analysis_before = analyze_historical_due_date_rules(pairs_before, statement_day=15)
    assert analysis_before.classification == HistoryRuleClassification.CONFLICTING
    assert analysis_before.survivors == []


def test_pure_one_sample_insufficient():
    """Fewer than 2 applicable complete pairs is INSUFFICIENT."""
    pairs_one = [(date(2021, 4, 25), date(2021, 5, 15))]
    analysis = analyze_historical_due_date_rules(pairs_one, statement_day=25)
    assert analysis.classification == HistoryRuleClassification.INSUFFICIENT
    assert analysis.applicable_pair_count == 1
    assert analysis.rule is None


def test_pure_missing_dates_are_non_applicable():
    """Pairs with None in statement_date or due_date are skipped as non-applicable."""
    pairs = [
        (None, date(2021, 5, 15)),
        (date(2021, 4, 25), None),
        (None, None),
        (date(2021, 7, 25), date(2021, 8, 14)),
    ]
    analysis = analyze_historical_due_date_rules(pairs, statement_day=25)
    assert analysis.classification == HistoryRuleClassification.INSUFFICIENT
    assert analysis.applicable_pair_count == 1
    assert analysis.rule is None


def test_pure_duplicate_conflicting_facts():
    """Same statement date with two conflicting due dates eliminates all candidates."""
    pairs = [
        (date(2024, 6, 5), date(2024, 6, 25)),
        (date(2024, 6, 5), date(2024, 6, 20)),
    ]
    analysis = analyze_historical_due_date_rules(pairs, statement_day=5)
    assert analysis.classification == HistoryRuleClassification.CONFLICTING
    assert analysis.applicable_pair_count == 2
    assert analysis.survivors == []


def test_pure_identical_duplicates_are_harmless():
    """Identical duplicate facts match the same candidates."""
    pairs = [
        (date(2021, 4, 25), date(2021, 5, 15)),
        (date(2021, 4, 25), date(2021, 5, 15)),
        (date(2021, 7, 25), date(2021, 8, 14)),
    ]
    analysis = analyze_historical_due_date_rules(pairs, statement_day=25)
    assert analysis.classification == HistoryRuleClassification.UNIQUE
    assert analysis.applicable_pair_count == 3
    assert analysis.survivors == [CandidateRule("days_after_statement", 20)]
    assert analysis.rule == CandidateRule("days_after_statement", 20)


# ---------------------------------------------------------------------------
# 2. API integration tests & Mandatory Tests 1, 2, 3, 5, 6
# ---------------------------------------------------------------------------

def test_api_mandatory_3_bank_rule_unavailable_with_unique_history_resolves_card_history(client, auth_headers, monkeypatch):
    """MANDATORY TEST 3: When bank-level rule is unavailable, unique compatible history resolves +20/card_history."""
    monkeypatch.setattr("app.services.due_date_rule_resolver.resolve_due_date_rule", lambda *args, **kwargs: None)
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="TEST_BANK", bank_name="测试银行", display_name="Test Card",
            last4="8759", statement_day=25, statement_day_boundary="uncertain",
            due_date_rule_type="observed_only", due_date_rule_value=None,
        )
        session.add(card)
        session.flush()

        session.add_all([
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2021, 4, 25), due_date=date(2021, 5, 15), total_due_minor=1000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2021, 7, 25), due_date=date(2021, 8, 14), total_due_minor=2000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2021, 10, 25), due_date=date(2021, 11, 14), total_due_minor=3000),
        ])
        session.commit()
        card_id = card.id

    res = client.get(f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-10", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is True
    assert data["mode"] == "future_estimate"
    assert data["estimate"]["rule_source"] == "card_history"
    assert data["estimate"]["selected_statement_date"] == "2026-09-25"
    assert data["estimate"]["selected_due_date"] == "2026-10-15"


def test_api_mandatory_1_bank_rule_unavailable_stale_history_with_current_day10_unavailable(client, auth_headers, monkeypatch):
    """MANDATORY TEST 1: When bank-level rule is unavailable, stale UNIQUE day25/+20 history + current statement_day10

    + zero compatible day10 rows => no card_history, unavailable/INSUFFICIENT (reason=unresolved_due_date_rule).
    """
    monkeypatch.setattr("app.services.due_date_rule_resolver.resolve_due_date_rule", lambda *args, **kwargs: None)
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="TEST_BANK", bank_name="测试银行", display_name="Test Card",
            last4="8759", statement_day=10, statement_day_boundary="uncertain",
            due_date_rule_type="observed_only", due_date_rule_value=None,
        )
        session.add(card)
        session.flush()

        # Statements are all on day 25, none compatible with current statement_day=10
        session.add_all([
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2021, 4, 25), due_date=date(2021, 5, 15), total_due_minor=1000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2021, 7, 25), due_date=date(2021, 8, 14), total_due_minor=2000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2021, 10, 25), due_date=date(2021, 11, 14), total_due_minor=3000),
        ])
        session.commit()
        card_id = card.id

    res = client.get(f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-05", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is False
    assert data["reason"] == "unresolved_due_date_rule"
    assert data["estimate"] is None


def test_api_mandatory_2_bank_rule_unavailable_patch_statement_day_resets_and_prevents_stale_history_resurrection(client, auth_headers, monkeypatch):
    """MANDATORY TEST 2: PATCH end-to-end when bank-level rule is unavailable:

    1. Unique day25 evidence initially resolves +20/card_history.
    2. PATCH statement_day25→10 resets explicit fields and sets statement_day=10.
    3. Subsequent estimate is unavailable (unresolved_due_date_rule); old day25 history cannot resurrect +20.
    """
    monkeypatch.setattr("app.services.due_date_rule_resolver.resolve_due_date_rule", lambda *args, **kwargs: None)
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="TEST_BANK", bank_name="测试银行", display_name="Test Card",
            last4="8759", statement_day=25, statement_day_boundary="uncertain",
            due_date_rule_type="observed_only", due_date_rule_value=None,
        )
        session.add(card)
        session.flush()

        session.add_all([
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2021, 4, 25), due_date=date(2021, 5, 15), total_due_minor=1000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2021, 7, 25), due_date=date(2021, 8, 14), total_due_minor=2000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2021, 10, 25), due_date=date(2021, 11, 14), total_due_minor=3000),
        ])
        session.commit()
        card_id = card.id

    # 1. Before PATCH: estimate is available via unique card_history
    res_before = client.get(f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-10", headers=auth_headers)
    assert res_before.status_code == 200
    assert res_before.json()["available"] is True
    assert res_before.json()["estimate"]["rule_source"] == "card_history"

    # 2. PATCH statement_day from 25 to 10
    patch_res = client.patch(f"/api/v1/cards/{card_id}", json={"statement_day": 10}, headers=auth_headers)
    assert patch_res.status_code == 200
    patched_card = patch_res.json()
    assert patched_card["statement_day"] == 10
    assert patched_card["due_date_rule_type"] == "observed_only"
    assert patched_card["due_date_rule_value"] is None
    assert patched_card["due_day"] is None

    # 3. Subsequent estimate: unavailable because day 25 history does not match statement_day=10
    res_after = client.get(f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-05", headers=auth_headers)
    assert res_after.status_code == 200
    data_after = res_after.json()
    assert data_after["available"] is False
    assert data_after["reason"] == "unresolved_due_date_rule"
    assert data_after["estimate"] is None

def test_api_icbc9980_current_day1_regime_resolves_fixed_day25(client, auth_headers):
    """Old rows are excluded; physical month ends normalize to day-1 anchors."""
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="ICBC", bank_name="工商银行", display_name="ICBC Card",
            last4="9980", statement_day=1, statement_day_boundary="next_cycle",
            due_date_rule_type="observed_only", due_date_rule_value=None,
        )
        session.add(card)
        session.flush()

        session.add_all([
            # Older day11 statements
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2017, 5, 11), due_date=date(2017, 6, 5), total_due_minor=1000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2017, 6, 11), due_date=date(2017, 7, 5), total_due_minor=1500),
            # Current natural-month physical records; logical anchors are next-day 1st.
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2017, 12, 31), due_date=date(2018, 1, 25), total_due_minor=2000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2018, 1, 31), due_date=date(2018, 2, 25), total_due_minor=2500),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2018, 2, 28), due_date=date(2018, 3, 25), total_due_minor=3000),
        ])
        session.commit()
        card_id = card.id

    res = client.get(f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-10", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is True
    assert data["reason"] is None
    assert data["estimate"]["rule_source"] == "card_history"
    assert data["estimate"]["selected_statement_date"] == "2026-10-01"
    assert data["estimate"]["selected_due_date"] == "2026-10-25"


def test_api_mandatory_6_filtering_is_read_only_and_rows_remain_unchanged(client, auth_headers):
    """MANDATORY TEST 6: Regime filtering is strictly read-only; old regime rows remain in DB unchanged."""
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="ICBC", bank_name="工商银行", display_name="ICBC Card",
            last4="9980", statement_day=1, statement_day_boundary="next_cycle",
            due_date_rule_type="observed_only", due_date_rule_value=None,
        )
        session.add(card)
        session.flush()

        stmt1 = Statement(card=card, bank_code=card.bank_code, statement_date=date(2017, 5, 11), due_date=date(2017, 6, 5), total_due_minor=1000)
        stmt2 = Statement(card=card, bank_code=card.bank_code, statement_date=date(2017, 12, 31), due_date=date(2018, 1, 25), total_due_minor=2000)
        stmt3 = Statement(card=card, bank_code=card.bank_code, statement_date=date(2018, 1, 31), due_date=date(2018, 2, 25), total_due_minor=2500)
        session.add_all([stmt1, stmt2, stmt3])
        session.commit()
        card_id = card.id

    def snapshot():
        with client.app.state.session_factory() as session:
            cards_state = session.execute(select(Card.__table__).where(Card.id == card_id)).mappings().all()
            stmts_state = session.execute(
                select(Statement.__table__).where(Statement.card_id == card_id).order_by(Statement.statement_date.asc())
            ).mappings().all()
            return [dict(r) for r in cards_state], [dict(r) for r in stmts_state]

    cards_before, stmts_before = snapshot()
    assert len(stmts_before) == 3

    # Call estimate API
    res = client.get(f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-10", headers=auth_headers)
    assert res.status_code == 200

    cards_after, stmts_after = snapshot()
    # Confirm absolutely zero DB writes or deletions
    assert cards_after == cards_before
    assert stmts_after == stmts_before
    # Specifically confirm the older day11 statement is intact
    assert any(s["statement_date"] == date(2017, 5, 11) for s in stmts_after)


def test_api_ambiguity_remains_unavailable_and_distinguishable(client, auth_headers):
    """P0-C: True divergence on target_statement_date remains unavailable.

    Day 30 card with 30-day-month statements (4/30->5/20, 6/30->7/20, 9/30->10/20):
    Survivors are fixed_day=20 and days_after_statement=20.

    - Spend in 31-day month (spend_date=2026-01-10 -> target stmt 2026-01-30):
      fixed_day=20 predicts 2026-02-20, while days_after=20 predicts 2026-02-19.
      Truly divergent -> available=False, reason=ambiguous_due_date_rule.

    - Spend in 30-day month (spend_date=2026-09-10 -> target stmt 2026-09-30):
      Both fixed_day=20 and days_after=20 predict 2026-10-20.
      Target-date identical -> available=True, selected_due_date=2026-10-20.
    """
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CGB", bank_name="广发银行", display_name="CGB Card",
            last4="1403", statement_day=30, statement_day_boundary="uncertain",
            due_date_rule_type="observed_only", due_date_rule_value=None,
        )
        session.add(card)
        session.flush()

        session.add_all([
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2024, 4, 30), due_date=date(2024, 5, 20), total_due_minor=1000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2024, 6, 30), due_date=date(2024, 7, 20), total_due_minor=2000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2024, 9, 30), due_date=date(2024, 10, 20), total_due_minor=3000),
        ])
        session.commit()
        card_id = card.id

    # 1. Truly divergent on target date -> unavailable
    res_divergent = client.get(
        f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-01-10",
        headers=auth_headers,
    )
    assert res_divergent.status_code == 200
    data_div = res_divergent.json()
    assert data_div["available"] is False
    assert data_div["mode"] == "future_estimate"
    assert data_div["reason"] == "ambiguous_due_date_rule"
    assert data_div["estimate"] is None

    # 2. Target date identical (even though long-term divergent) -> resolved
    res_resolved = client.get(
        f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-10",
        headers=auth_headers,
    )
    assert res_resolved.status_code == 200
    data_res = res_resolved.json()
    assert data_res["available"] is True
    assert data_res["mode"] == "future_estimate"
    assert data_res["estimate"]["selected_due_date"] == "2026-10-20"
    assert data_res["estimate"]["rule_source"] == "card_history"


def test_api_same_month_ambiguity_resolves_via_card_history(client, auth_headers):
    """CGB/CITIC-like same-month ambiguity (statement 5 -> due 25) has fully
    determined future behaviour and resolves via card_history (+20).
    """
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CGB", bank_name="广发银行", display_name="CGB Card",
            last4="1402", statement_day=5, statement_day_boundary="uncertain",
            due_date_rule_type="observed_only", due_date_rule_value=None,
        )
        session.add(card)
        session.flush()

        session.add_all([
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2024, 6, 5), due_date=date(2024, 6, 25), total_due_minor=1000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2024, 11, 5), due_date=date(2024, 11, 25), total_due_minor=2000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2025, 1, 5), due_date=date(2025, 1, 25), total_due_minor=3000),
        ])
        session.commit()
        card_id = card.id

    res = client.get(f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-10", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is True
    assert data["estimate"]["rule_source"] == "card_history"
    # spend 2026-09-10 > statement day 5 -> next cycle statement 2026-10-05, +20 -> 2026-10-25
    assert data["estimate"]["selected_statement_date"] == "2026-10-05"
    assert data["estimate"]["selected_due_date"] == "2026-10-25"


def test_api_active_regime_conflict_remains_unavailable(client, auth_headers):
    """Card with conflicting statements within the active regime returns conflicting_due_date_rule."""
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="ICBC", bank_name="工商银行", display_name="ICBC Card",
            last4="9981", statement_day=25, statement_day_boundary="uncertain",
            due_date_rule_type="observed_only", due_date_rule_value=None,
        )
        session.add(card)
        session.flush()

        # Within the statement_day=25 regime, two conflicting due dates
        session.add_all([
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2024, 4, 25), due_date=date(2024, 5, 15), total_due_minor=1000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2024, 5, 25), due_date=date(2024, 6, 25), total_due_minor=2000),
        ])
        session.commit()
        card_id = card.id

    res = client.get(f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-10", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is False
    assert data["mode"] == "future_estimate"
    assert data["reason"] == "conflicting_due_date_rule"
    assert data["estimate"] is None


def test_api_precedence_explicit_card_rule_highest(client, auth_headers):
    """Architecture precedence: explicit Card rule wins over confirmed bank facts and history."""
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CMB", bank_name="招商银行", display_name="CMB Card",
            last4="9492", statement_day=9, statement_day_boundary="uncertain",
            due_date_rule_type="days_after_statement", due_date_rule_value=20,
        )
        session.add(card)
        session.flush()

        session.add_all([
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2026, 7, 9), due_date=date(2026, 7, 27), total_due_minor=1000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2026, 8, 9), due_date=date(2026, 8, 27), total_due_minor=2000),
        ])
        session.commit()
        card_id = card.id

    res = client.get(f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-09", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is True
    assert data["estimate"]["rule_source"] == "explicit_card"
    assert data["estimate"]["selected_due_date"] == "2026-09-29"


def test_api_precedence_card_history_wins_before_bank_rule(client, auth_headers):
    """Architecture precedence (Phase 3): determined card history wins before
    evaluating bank known rules. CMB 1631: history +18 resolves even though the
    CMB map would give fixed_day=27.
    """
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CMB", bank_name="招商银行", display_name="CMB Card",
            last4="1631", statement_day=9, statement_day_boundary="uncertain",
            due_date_rule_type="observed_only", due_date_rule_value=None,
        )
        session.add(card)
        session.flush()

        session.add_all([
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2026, 7, 9), due_date=date(2026, 7, 27), total_due_minor=1000),
            Statement(card=card, bank_code=card.bank_code, statement_date=date(2026, 8, 9), due_date=date(2026, 8, 27), total_due_minor=2000),
        ])
        session.commit()
        card_id = card.id

    res = client.get(f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-09", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is True
    assert data["estimate"]["rule_source"] == "card_history"
    # History +18: 2026-09-09 statement -> 2026-09-27 due (same dates as the
    # map's fixed 27 here, but provenance is card_history).
    assert data["estimate"]["selected_due_date"] == "2026-09-27"


def test_walk_forward_history_isolation_prevents_future_leakage(client, auth_headers):
    """P0-D: Passing history_pairs strictly bounds the historical analysis scope.

    Statements beyond the slice are invisible to candidate elimination.
    """
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="BOC", bank_name="中国银行", display_name="BOC Card",
            last4="8889", statement_day=25, statement_day_boundary="uncertain",
            due_date_rule_type="observed_only", due_date_rule_value=None,
        )
        session.add(card)
        session.flush()

        # Database contains 3 statements
        s1 = Statement(card=card, bank_code=card.bank_code, statement_date=date(2026, 5, 25), due_date=date(2026, 6, 14), total_due_minor=1000)
        s2 = Statement(card=card, bank_code=card.bank_code, statement_date=date(2026, 6, 25), due_date=date(2026, 7, 15), total_due_minor=1000)
        s3 = Statement(card=card, bank_code=card.bank_code, statement_date=date(2026, 7, 25), due_date=date(2026, 8, 14), total_due_minor=1000)
        session.add_all([s1, s2, s3])
        session.commit()

        from app.services.due_date_rule_resolver import resolve_effective_due_rule

        # Passing only 1 prior pair -> INSUFFICIENT history (falls to BOC confirmed +20 bank rule)
        res_1 = resolve_effective_due_rule(
            session, card,
            target_statement_date=date(2026, 6, 25),
            history_pairs=[(s1.statement_date, s1.due_date)],
        )
        assert res_1.rule is not None
        assert res_1.rule.rule_source == "bank"

        # Passing 2 prior pairs -> history tier resolves +20 directly
        res_2 = resolve_effective_due_rule(
            session, card,
            target_statement_date=date(2026, 7, 25),
            history_pairs=[(s1.statement_date, s1.due_date), (s2.statement_date, s2.due_date)],
        )
        assert res_2.rule is not None
        assert res_2.rule.rule_source == "history"


def test_day31_month_end_clamp_isolation_from_bank_dictionary() -> None:
    """P0-A: statement_day=31 clamps to month-ends in history analysis,

    but remains correctly rejected at the bank dictionary layer because ICBC
    official rules only support statement days 14..28 and legacy 1.
    """
    from app.services.bank_billing_rules import resolve_due_date_rule

    # Bank dictionary refuses statement_day=31 (not an official selectable day)
    assert resolve_due_date_rule("ICBC", statement_day=31) is None

    # History analyzer accepts month-end statements under canonical 31 regime
    pairs = [
        (date(2026, 1, 31), date(2026, 2, 25)),
        (date(2026, 2, 28), date(2026, 3, 25)),
        (date(2026, 4, 30), date(2026, 5, 25)),
    ]
    analysis = analyze_historical_due_date_rules(pairs, statement_day=31)
    assert analysis.rule is not None
    assert analysis.rule.rule_type == "days_after_statement"
    assert analysis.rule.rule_value == 25


def test_last_verified_date_only_uses_effective_evidence() -> None:
    """Verify last_verified_date strictly uses effective evidence, ignoring newer excluded/invalid statements."""
    pairs = [
        (date(2026, 6, 25), date(2026, 7, 15)),   # +20 days (effective evidence 1)
        (date(2026, 7, 25), date(2026, 8, 14)),   # +20 days (effective evidence 2, latest valid)
        (date(2026, 8, 10), date(2026, 8, 30)),   # Newer, but excluded regime (day 10 != statement_day 25)
        (None, date(2026, 9, 15)),                # Newer, but invalid statement_date
        (date(2026, 9, 25), None),                # Newer, but invalid due_date
    ]
    analysis = analyze_historical_due_date_rules(pairs, statement_day=25)
    assert analysis.rule is not None
    assert analysis.rule.rule_type == "days_after_statement"
    assert analysis.rule.rule_value == 20
    assert analysis.applicable_pair_count == 2
    assert analysis.excluded_regime_pair_count == 1
    # Must point to the latest valid effective evidence date (2026-07-25), not 2026-08-10 or 2026-09-25
    assert analysis.last_verified_date == date(2026, 7, 25)
