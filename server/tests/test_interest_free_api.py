from datetime import date

import pytest
from sqlalchemy import select, text

from app.api.v1 import cards as cards_api
from app.api.v1 import transactions as transactions_api
from app.core.plain_secrets import set_secret
from app.models.card import Card
from app.models.statement import Statement
from app.models.transaction import Transaction


@pytest.fixture
def interest_free_data(client):
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="TEST", bank_name="Test Bank", display_name="Test Card",
            last4="1234", statement_day=9, due_day=27,
            due_date_rule_type="days_after_statement", due_date_rule_value=18,
        )
        statement = Statement(
            bank_code="CMB",
            card=card, statement_date=date(2026, 9, 9),
            due_date=date(2026, 9, 27), total_due_minor=10000,
        )
        transaction = Transaction(
            statement=statement, transaction_date=date(2026, 9, 1),
            posting_date=date(2026, 9, 2), amount_minor=10000,
            currency="CNY", raw_description="Synthetic purchase",
        )
        session.add(transaction)
        session.commit()
        return card.id, statement.id, transaction.id


def _paths(data):
    card_id, _, transaction_id = data
    return (
        f"/api/v1/transactions/{transaction_id}/interest-free",
        f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-09",
    )


def test_historical_uses_relation_and_actual_due_despite_future_rules(
    client, auth_headers, interest_free_data,
):
    card_id, _, _ = interest_free_data
    with client.app.state.session_factory() as session:
        card = session.get(Card, card_id)
        card.statement_day = 1
        card.due_date_rule_type = "fixed_day"
        card.due_date_rule_value = 6
        session.add(Statement(
            bank_code="CMB",
            card=card, statement_date=date(2026, 9, 1),
            due_date=date(2026, 9, 6), total_due_minor=1,
        ))
        session.commit()

    response = client.get(_paths(interest_free_data)[0], headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {
        "available": True, "mode": "historical_actual", "reason": None,
        "estimate": {
            "card_id": card_id, "spend_date": "2026-09-01",
            "statement_date": "2026-09-09",
            "due_date": "2026-09-27",
            "interest_free_days": 26,
            "posting_date": "2026-09-02",
            "estimation_level": "actual",
        },
    }


def test_missing_posting_remains_actual(client, auth_headers, interest_free_data):
    with client.app.state.session_factory() as session:
        session.get(Transaction, interest_free_data[2]).posting_date = None
        session.commit()
    response = client.get(_paths(interest_free_data)[0], headers=auth_headers)
    assert response.status_code == 200
    result = response.json()["estimate"]
    assert result["estimation_level"] == "actual"
    assert result["posting_date"] is None
    assert result["interest_free_days"] == 26


@pytest.mark.parametrize("transaction_date,posting_date,reason", [
    (None, None, "missing_transaction_date"),
    (None, date(2026, 9, 2), "missing_transaction_date"),
    (date(2026, 9, 28), date(2026, 9, 29), "due_date_before_transaction_date"),
    (date(2026, 9, 28), None, "due_date_before_transaction_date"),
])
def test_historical_insufficient_facts(
    client, auth_headers, interest_free_data, transaction_date, posting_date, reason,
):
    with client.app.state.session_factory() as session:
        transaction = session.get(Transaction, interest_free_data[2])
        transaction.transaction_date = transaction_date
        transaction.posting_date = posting_date
        session.commit()
    response = client.get(_paths(interest_free_data)[0], headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {
        "available": False, "mode": "historical_actual", "reason": reason,
        "estimate": None,
    }


@pytest.mark.parametrize("rule,value,due_day,expected_stmt,expected_due,expected_days", [
    ("days_after_statement", 18, 1, "2026-09-09", "2026-09-27", 18),
    ("fixed_day", 6, 1, "2026-09-09", "2026-10-06", 27),
])
def test_future_rules_and_conservative_forecast(
    client, auth_headers, interest_free_data, rule, value, due_day, expected_stmt, expected_due, expected_days,
):
    with client.app.state.session_factory() as session:
        card = session.get(Card, interest_free_data[0])
        card.due_date_rule_type, card.due_date_rule_value = rule, value
        card.due_day = due_day
        session.commit()
    response = client.get(_paths(interest_free_data)[1], headers=auth_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["mode"] == "future_estimate"
    assert payload["reason"] is None
    result = payload["estimate"]
    assert result["card_id"] == interest_free_data[0]
    assert result["spend_date"] == "2026-09-09"
    assert result["estimation_level"] == "fully_estimated"
    assert result["selected_statement_date"] == expected_stmt
    assert result["selected_due_date"] == expected_due
    assert result["conservative_interest_free_days"] == expected_days
    assert result["conservative"] is True
    assert result["boundary_state"] == "uncertain"
    assert isinstance(result["theoretical_max_interest_free_days"], int)


@pytest.mark.parametrize("changes,reason", [
    ({"statement_day": None}, "missing_statement_day"),
    ({"statement_day": 0}, "invalid_statement_day"),
    ({"statement_day": 32}, "invalid_statement_day"),
    ({"due_date_rule_value": None}, "invalid_due_date_rule"),
    ({"due_date_rule_value": 0}, "invalid_due_date_rule"),
    ({"due_date_rule_value": 101}, "invalid_due_date_rule"),
    ({"due_date_rule_type": "fixed_day", "due_date_rule_value": None}, "invalid_due_date_rule"),
    ({"due_date_rule_type": "fixed_day", "due_date_rule_value": 0}, "invalid_due_date_rule"),
    ({"due_date_rule_type": "fixed_day", "due_date_rule_value": 32}, "invalid_due_date_rule"),
    ({"due_date_rule_type": "unknown"}, "invalid_due_date_rule"),
    ({"due_date_rule_type": "observed_only", "due_date_rule_value": 25}, "invalid_due_date_rule"),
    ({"due_date_rule_type": "observed_only", "due_date_rule_value": None, "due_day": None}, "unresolved_due_date_rule"),
    ({"due_date_rule_type": "observed_only", "due_date_rule_value": None, "due_day": 25}, "unresolved_due_date_rule"),
])
def test_future_unavailable_without_valid_evidence(
    client, auth_headers, interest_free_data, changes, reason,
):
    with client.app.state.session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints = ON"))
        card = session.get(Card, interest_free_data[0])
        for name, value in changes.items():
            setattr(card, name, value)
        session.flush()
        session.execute(text("PRAGMA ignore_check_constraints = OFF"))
        session.commit()
    response = client.get(_paths(interest_free_data)[1], headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {
        "available": False, "mode": "future_estimate", "reason": reason,
        "estimate": None,
    }
    historical = client.get(_paths(interest_free_data)[0], headers=auth_headers)
    assert historical.status_code == 200
    assert historical.json()["estimate"]["due_date"] == "2026-09-27"


def test_patch_statement_day_invalidates_due_pattern(client, auth_headers, interest_free_data):
    card_id = interest_free_data[0]
    res = client.get(_paths(interest_free_data)[1], headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["available"] is True

    patch_same = client.patch(f"/api/v1/cards/{card_id}", json={"statement_day": 9}, headers=auth_headers)
    assert patch_same.status_code == 200
    res_same = client.get(_paths(interest_free_data)[1], headers=auth_headers)
    assert res_same.json()["available"] is True

    patch_diff = client.patch(f"/api/v1/cards/{card_id}", json={"statement_day": 15}, headers=auth_headers)
    assert patch_diff.status_code == 200
    card_after = patch_diff.json()
    assert card_after["due_date_rule_type"] == "observed_only"
    assert card_after["due_date_rule_value"] is None
    assert card_after["statement_day_boundary"] == "uncertain"
    assert card_after["due_day"] is None

    res_invalid = client.get(f"/api/v1/cards/{card_id}/interest-free-estimate?spend_date=2026-09-09", headers=auth_headers)
    assert res_invalid.status_code == 200
    assert res_invalid.json()["available"] is False
    assert res_invalid.json()["reason"] == "unresolved_due_date_rule"


def test_missing_resources_and_invalid_spend_date(client, auth_headers):
    for path in _paths(("missing", "missing", "missing")):
        assert client.get(path, headers=auth_headers).status_code == 404
    base = "/api/v1/cards/missing/interest-free-estimate"
    for suffix in ("", "?spend_date=invalid", "?spend_date=2026-02-30", "?spend_date=9999-12-31"):
        assert client.get(base + suffix, headers=auth_headers).status_code == 422


def test_agent_read_authorization(client, auth_headers, interest_free_data):
    token = "synthetic-interest-free-agent-token-32-chars"
    set_secret("agent", "token", token)

    for path in _paths(interest_free_data):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert client.get(path, headers=auth_headers).status_code == 200
        response = client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["available"] is True


def test_read_only_business_state(client, auth_headers, interest_free_data):
    def snapshot():
        with client.app.state.session_factory() as session:
            return [session.execute(select(model.__table__)).all() for model in (Card, Statement, Transaction)]

    before = snapshot()
    for path in _paths(interest_free_data):
        client.get(path, headers=auth_headers)
    assert snapshot() == before
