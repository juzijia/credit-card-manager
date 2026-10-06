from datetime import date, timedelta

from app.models.repayment import Repayment
from app.models.statement import Statement
from app.services.statements import remaining_minor, status_for


def make_statement(due_date: date, total: int = 10000) -> Statement:
    statement = Statement(bank_code="CMB",
        card_id="card-id",
        statement_date=due_date - timedelta(days=20),
        due_date=due_date,
        total_due_minor=total,
        currency="CNY",
    )
    statement.repayments = []
    return statement


def test_pending_and_overdue_status_are_dynamic() -> None:
    today = date(2026, 9, 6)
    pending = make_statement(today + timedelta(days=1))
    overdue = make_statement(today - timedelta(days=1))

    assert status_for(pending, today) == "pending"
    assert status_for(overdue, today) == "overdue_hint"


def test_remaining_never_becomes_negative() -> None:
    statement = make_statement(date(2026, 9, 6), total=10000)
    statement.repayments = [
        Repayment(
            statement_id="statement-id",
            amount_minor=12000,
            idempotency_key="test-overpay",
        )
    ]
    assert remaining_minor(statement) == 0
