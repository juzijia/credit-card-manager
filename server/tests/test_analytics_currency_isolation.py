"""Analytics currency-isolation regression tests (Phase 2).

Subagent C audit freeze: every analytics query path filters
`currency == "CNY"` at the SQL layer (cards, statements, transactions,
repayments). These tests prove that foreign-currency rows never leak
into any aggregate even when they exist in the database.
"""

from datetime import date, datetime
import pytest
from fastapi.testclient import TestClient

from app.models.card import Card
from app.models.repayment import Repayment
from app.models.statement import ParseStatus, Statement
from app.models.transaction import Transaction


@pytest.fixture(autouse=True)
def _freeze_currency_isolation_date(monkeypatch):
    frozen_date = date(2026, 9, 15)
    for target in (
        "app.services.statements.local_today",
        "app.services.analytics.local_today",
        "app.services.system_settings.local_today",
        "app.api.v1.dashboard.local_today",
        "app.api.v1.cards.local_today",
    ):
        monkeypatch.setattr(target, lambda *args, **kwargs: frozen_date)


def _add_card(session, *, last4: str, currency: str = "CNY") -> Card:
    card = Card(
        bank_code="CMB",
        bank_name="招商银行",
        display_name=f"招商银行 ****{last4}",
        last4=last4,
        currency=currency,
    )
    session.add(card)
    session.flush()
    return card


def _add_statement(
    session,
    *,
    card_id: str,
    total_due_minor: int,
    currency: str = "CNY",
    due_date: date = date(2026, 9, 28),
    parse_status: ParseStatus = ParseStatus.SUMMARY_OK,
) -> Statement:
    statement = Statement(
        bank_code="CMB",
        card_id=card_id,
        statement_date=date(2026, 8, 10),
        due_date=due_date,
        total_due_minor=total_due_minor,
        minimum_due_minor=min(total_due_minor, 100),
        currency=currency,
        parse_status=parse_status,
    )
    session.add(statement)
    session.flush()
    return statement


def _add_transaction(
    session, *, statement_id: str, amount_minor: int, currency: str = "CNY"
) -> Transaction:
    transaction = Transaction(
        statement_id=statement_id,
        transaction_date=date(2026, 8, 15),
        posting_date=date(2026, 8, 16),
        amount_minor=amount_minor,
        currency=currency,
        raw_description="test purchase",
        transaction_type="purchase",
    )
    session.add(transaction)
    session.flush()
    return transaction


def _get_analytics(client: TestClient, auth_headers: dict[str, str]) -> dict:
    response = client.get("/api/v1/analytics", headers=auth_headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_foreign_transactions_excluded_from_monthly_consumption(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    with client.app.state.session_factory() as session:
        card = _add_card(session, last4="2001")
        cny_stmt = _add_statement(session, card_id=card.id, total_due_minor=1000)
        usd_stmt = _add_statement(session, card_id=card.id, total_due_minor=2000, currency="USD")
        _add_transaction(session, statement_id=cny_stmt.id, amount_minor=30000)
        _add_transaction(session, statement_id=usd_stmt.id, amount_minor=8888888, currency="USD")
        session.commit()

    payload = _get_analytics(client, auth_headers)
    months = {m["month"]: m for m in payload["monthly_consumption"]}
    assert months["2026-08"]["amount_minor"] == 30000
    assert months["2026-08"]["transaction_count"] == 1
    assert months["2026-08"]["gross_purchase_minor"] == 30000


def test_foreign_statement_excluded_from_due_and_utilization(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    with client.app.state.session_factory() as session:
        cny_card = _add_card(session, last4="2002", currency="CNY")
        usd_card = _add_card(session, last4="2003", currency="USD")
        _add_statement(session, card_id=cny_card.id, total_due_minor=40000)
        _add_statement(
            session, card_id=usd_card.id, total_due_minor=7777777, currency="USD"
        )
        session.commit()

    payload = _get_analytics(client, auth_headers)
    months = {m["month"]: m for m in payload["monthly_due"]}
    assert months["2026-09"]["credit_card_remaining_minor"] == 40000

    for entry in payload["card_utilization"]:
        assert entry["outstanding_minor"] != 7777777
    for entry in payload["bank_utilization"]:
        assert entry["outstanding_minor"] != 7777777


def test_foreign_repayment_excluded_from_repayment_trend(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    with client.app.state.session_factory() as session:
        cny_card = _add_card(session, last4="2004")
        usd_card = _add_card(session, last4="2005", currency="USD")
        cny_stmt = _add_statement(session, card_id=cny_card.id, total_due_minor=50000)
        usd_stmt = _add_statement(
            session, card_id=usd_card.id, total_due_minor=60000, currency="USD"
        )
        session.add_all(
            [
                Repayment(
                    statement_id=cny_stmt.id,
                    amount_minor=10000,
                    paid_at=datetime(2026, 8, 20, 8, 0, 0),
                    idempotency_key="rep-cny-1",
                ),
                Repayment(
                    statement_id=usd_stmt.id,
                    amount_minor=9999999,
                    paid_at=datetime(2026, 8, 20, 9, 0, 0),
                    idempotency_key="rep-usd-1",
                ),
            ]
        )
        session.commit()

    payload = _get_analytics(client, auth_headers)
    months = {m["month"]: m for m in payload["repayment_trend"]}
    assert months["2026-08"]["credit_card_paid_minor"] == 10000
