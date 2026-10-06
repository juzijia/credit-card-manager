from datetime import date

import pytest

from app.api.v1 import cards as cards_api
from app.models.card import Card
from app.models.statement import Statement
from app.models.transaction import Transaction


def _card(session, bank_code, last4, statement_day, rule_type, rule_value=None, due_day=None):
    card = Card(
        bank_code=bank_code, bank_name=bank_code, display_name=last4,
        last4=last4, currency="CNY", statement_day=statement_day,
        due_date_rule_type=rule_type, due_date_rule_value=rule_value, due_day=due_day,
    )
    session.add(card)
    session.flush()
    return card


@pytest.mark.parametrize(
    ("today", "month", "rule_type", "rule_value", "expected_statement", "expected_due"),
    [
        (date(2023, 1, 1), "2023-02-01", "fixed_day", 31, "2023-01-31", "2023-02-28"),
        (date(2024, 1, 1), "2024-02-01", "fixed_day", 31, "2024-01-31", "2024-02-29"),
        (date(2024, 1, 1), "2024-02-01", "days_after_statement", 20, "2024-01-31", "2024-02-20"),
        (date(2026, 3, 1), "2026-04-01", "fixed_day", 31, "2026-03-31", "2026-04-30"),
        (date(2025, 12, 1), "2026-01-01", "days_after_statement", 20, "2025-12-31", "2026-01-20"),
    ],
)
def test_calendar_estimate_uses_approved_rule_across_month_boundaries(
    client, auth_headers, monkeypatch, today, month, rule_type, rule_value,
    expected_statement, expected_due,
):
    monkeypatch.setattr(cards_api, "local_today", lambda _: today)
    with client.app.state.session_factory() as session:
        card = _card(session, "ZZZ", "1001", 31, rule_type, rule_value)
        card_id = card.id
        session.commit()

    response = client.get(f"/api/v1/cards/calendar-estimates?month={month}", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert {tuple(item[field] for field in ("card_id", "statement_date", "due_date")) for item in response.json()} >= {
        (card_id, expected_statement, expected_due)
    }


def test_calendar_estimate_does_not_promote_observed_due_day_or_duplicate_real_statement(
    client, auth_headers, monkeypatch,
):
    monkeypatch.setattr(cards_api, "local_today", lambda _: date(2026, 1, 1))
    with client.app.state.session_factory() as session:
        observed = _card(session, "ZZZ", "1001", 15, "observed_only", due_day=25)
        known = _card(session, "ZZZ", "1002", 15, "fixed_day", 28)
        statement = Statement(
            bank_code="ZZZ", card_id=known.id, statement_date=date(2026, 2, 15),
            due_date=date(2026, 2, 27), total_due_minor=1000, currency="CNY",
        )
        session.add(statement)
        session.commit()
        observed_id = observed.id
        known_id = known.id

    response = client.get("/api/v1/cards/calendar-estimates?month=2026-02-01", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert not any(item["card_id"] in {observed_id, known_id} for item in response.json())
    actual = client.get("/api/v1/statements", headers=auth_headers).json()
    assert any(item["card_id"] == known_id and item["due_date"] == "2026-02-27" for item in actual)
    assert client.get("/api/v1/cards/calendar-estimates?month=2026-02-02", headers=auth_headers).status_code == 422


def test_ccb_account_statement_remains_real_and_is_not_forecast_per_child(
    client, auth_headers, monkeypatch,
):
    monkeypatch.setattr(cards_api, "local_today", lambda _: date(2026, 1, 1))
    with client.app.state.session_factory() as session:
        first = _card(session, "CCB", "0183", 15, "fixed_day", 5)
        second = _card(session, "CCB", "6571", 15, "fixed_day", 5)
        account = Statement(
            bank_code="CCB", card_id=None, statement_date=date(2026, 2, 15),
            due_date=date(2026, 3, 5), total_due_minor=5000, currency="CNY",
        )
        session.add(account)
        session.flush()
        session.add(Transaction(
            statement_id=account.id, card_id=first.id,
            transaction_date=date(2026, 2, 2), posting_date=date(2026, 2, 3),
            amount_minor=1000, currency="CNY", raw_description="child purchase",
        ))
        session.commit()
        child_ids = {first.id, second.id}

    estimates = client.get("/api/v1/cards/calendar-estimates?month=2026-02-01", headers=auth_headers)
    assert estimates.status_code == 200, estimates.text
    assert not any(item["card_id"] in child_ids for item in estimates.json())
    actual = client.get("/api/v1/statements", headers=auth_headers).json()
    assert any(item["card_id"] is None and item["due_date"] == "2026-03-05" for item in actual)
