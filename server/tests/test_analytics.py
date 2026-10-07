from datetime import UTC, date, datetime, timedelta
import pytest
from fastapi.testclient import TestClient

from app.models.card import Card
from app.models.manual_bill import ManualBill, ManualBillPayment
from app.models.repayment import Repayment
from app.models.statement import ParseStatus, Statement
from app.models.transaction import Transaction


@pytest.fixture(autouse=True)
def _freeze_analytics_date(monkeypatch):
    frozen_date = date(2026, 9, 15)
    for target in (
        "app.services.statements.local_today",
        "app.services.analytics.local_today",
        "app.services.system_settings.local_today",
        "app.api.v1.dashboard.local_today",
        "app.api.v1.cards.local_today",
    ):
        monkeypatch.setattr(target, lambda *args, **kwargs: frozen_date)


def _month_start(value: date, offset: int = 0) -> date:
    absolute_month = value.year * 12 + value.month - 1 + offset
    return date(absolute_month // 12, absolute_month % 12 + 1, 1)


def test_analytics_uses_transaction_due_and_paid_dates(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    today = date(2026, 9, 15)
    current = _month_start(today)
    previous = _month_start(today, -1)
    paid_at = datetime(today.year, today.month, min(today.day, 15), 1, tzinfo=UTC).replace(
        tzinfo=None
    )

    with client.app.state.session_factory() as session:
        first = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="第一张卡",
            last4="1234",
            currency="CNY",
            credit_limit_minor=1_000_000,
        )
        second = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="第二张卡",
            last4="5678",
            currency="CNY",
            credit_limit_minor=800_000,
        )
        session.add_all([first, second])
        session.flush()
        statement = Statement(
            bank_code="CMB",
            card_id=first.id,
            statement_date=current,
            due_date=date(today.year, today.month, min(28, today.day + 10)),
            total_due_minor=30_000,
            minimum_due_minor=3_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        session.add(statement)
        session.flush()
        session.add_all(
            [
                Transaction(
                    statement_id=statement.id,
                    transaction_date=previous,
                    posting_date=current,
                    amount_minor=12_000,
                    currency="CNY",
                    raw_description="上月消费、本月入账",
                ),
                Transaction(
                    statement_id=statement.id,
                    transaction_date=previous,
                    posting_date=current,
                    amount_minor=-2_000,
                    currency="CNY",
                    raw_description="退款",
                ),
                Repayment(
                    statement_id=statement.id,
                    amount_minor=5_000,
                    paid_at=paid_at,
                    idempotency_key="analytics-repayment",
                ),
            ]
        )
        manual_bill = ManualBill(
            bill_type="房租",
            name="测试房租",
            statement_day=1,
            due_day=20,
            monthly_amount_minor=8_000,
        )
        session.add(manual_bill)
        session.flush()
        session.add(
            ManualBillPayment(
                manual_bill_id=manual_bill.id,
                period=current.strftime("%Y-%m"),
                due_date=date(today.year, today.month, 20),
                amount_minor=8_000,
            )
        )
        session.commit()

    response = client.get("/api/v1/analytics?months=2", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()

    previous_consumption = payload["monthly_consumption"][0]
    assert {
        key: previous_consumption[key]
        for key in (
            "month",
            "amount_minor",
            "transaction_count",
            "gross_purchase_minor",
            "refund_minor",
            "net_consumption_minor",
        )
    } == {
        "month": previous.strftime("%Y-%m"),
        "amount_minor": 10_000,
        "transaction_count": 1,
        "gross_purchase_minor": 12_000,
        "refund_minor": 2_000,
        "net_consumption_minor": 10_000,
    }
    assert previous_consumption["bank_usage"] == [
        {
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "amount_minor": 10_000,
            "credit_limit_minor": 1_000_000,
            "monthly_spend_to_limit_percent": 1.0,
            "utilization_percent": 1.0,
        }
    ]
    assert payload["monthly_consumption"][1]["bank_usage"][0]["amount_minor"] == 0
    assert payload["monthly_consumption"][1]["manual_bill_amount_minor"] == 8_000
    assert payload["monthly_consumption"][1]["manual_bill_count"] == 1
    assert payload["monthly_consumption"][1]["total_amount_minor"] == 8_000
    current_due = payload["monthly_due"][1]
    assert current_due["credit_card_remaining_minor"] == 25_000
    assert current_due["manual_bill_remaining_minor"] == 8_000
    assert current_due["total_remaining_minor"] == 33_000

    assert payload["bank_utilization"][0]["credit_limit_minor"] == 1_000_000
    assert payload["bank_utilization"][0]["outstanding_minor"] == 25_000
    assert payload["bank_utilization"][0]["utilization_percent"] == 2.5
    assert (
        payload["bank_utilization"][0]["outstanding_to_known_limit_percent"]
        == 2.5
    )
    assert {item["last4"] for item in payload["card_utilization"]} == {
        "1234",
        "5678",
    }
    assert payload["repayment_trend"][1]["credit_card_paid_minor"] == 5_000


def test_analytics_month_range_is_validated(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert client.get("/api/v1/analytics?months=0", headers=auth_headers).status_code == 422
    assert client.get("/api/v1/analytics?months=25", headers=auth_headers).status_code == 422


def test_repayment_trend_card_filter_keeps_registered_and_account_level_payments(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    today = date(2026, 9, 15)
    paid_at = datetime(today.year, today.month, 10, 8)
    schema = client.get("/openapi.json").json()
    parameters = schema["paths"]["/api/v1/analytics"]["get"]["parameters"]
    assert any(parameter["name"] == "repayment_card_id" for parameter in parameters)
    with client.app.state.session_factory() as session:
        first = Card(bank_code="CMB", bank_name="招商银行", display_name="first", last4="1001", currency="CNY")
        unpaid = Card(bank_code="CMB", bank_name="招商银行", display_name="unpaid", last4="1002", currency="CNY")
        ccb = Card(bank_code="CCB", bank_name="建设银行", display_name="CCB", last4="1003", currency="CNY")
        session.add_all([first, unpaid, ccb])
        session.flush()
        partial = Statement(bank_code="CMB", card_id=first.id, statement_date=today, due_date=today,
                            total_due_minor=30_000, minimum_due_minor=3_000, currency="CNY", parse_status=ParseStatus.COMPLETE)
        full = Statement(bank_code="CMB", card_id=first.id, statement_date=today - timedelta(days=1), due_date=today,
                         total_due_minor=20_000, minimum_due_minor=2_000, currency="CNY", parse_status=ParseStatus.COMPLETE)
        zero = Statement(bank_code="CMB", card_id=unpaid.id, statement_date=today, due_date=today,
                         total_due_minor=80_000, minimum_due_minor=8_000, currency="CNY", parse_status=ParseStatus.COMPLETE)
        account = Statement(bank_code="CCB", card_id=None, statement_date=today, due_date=today,
                            total_due_minor=7_000, minimum_due_minor=700, currency="CNY", parse_status=ParseStatus.COMPLETE)
        session.add_all([partial, full, zero, account])
        session.flush()
        session.add_all([
            Repayment(statement_id=partial.id, amount_minor=5_000, paid_at=paid_at, idempotency_key="partial"),
            Repayment(statement_id=full.id, amount_minor=20_000, paid_at=paid_at, idempotency_key="full"),
            Repayment(statement_id=account.id, amount_minor=7_000, paid_at=paid_at, idempotency_key="account"),
        ])
        session.commit()

    def paid(card_id=None):
        suffix = f"&repayment_card_id={card_id}" if card_id else ""
        response = client.get(f"/api/v1/analytics?months=1{suffix}", headers=auth_headers)
        assert response.status_code == 200, response.text
        return response.json()["repayment_trend"][0]["credit_card_paid_minor"]

    assert paid() == 32_000
    assert paid(first.id) == 25_000
    assert paid(unpaid.id) == 0
    assert paid(ccb.id) == 0


def test_analytics_consumption_excludes_non_purchases_and_offsets_refunds(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    today = date(2026, 9, 15)
    current = _month_start(today)

    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="BOCOM",
            bank_name="交通银行",
            display_name="交行测试卡",
            last4="8888",
            currency="CNY",
            credit_limit_minor=2_000_000,
        )
        session.add(card)
        session.flush()
        statement = Statement(
            bank_code="CMB",
            card_id=card.id,
            statement_date=current,
            due_date=date(today.year, today.month, min(28, today.day + 10)),
            total_due_minor=50_000,
            minimum_due_minor=5_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        session.add(statement)
        session.flush()

        session.add_all(
            [
                Transaction(
                    statement_id=statement.id,
                    transaction_date=current,
                    posting_date=current,
                    amount_minor=10_000,
                    currency="CNY",
                    raw_description="商户消费A",
                ),
                Transaction(
                    statement_id=statement.id,
                    transaction_date=current,
                    posting_date=current,
                    amount_minor=5_000,
                    currency="CNY",
                    raw_description="商户消费B",
                ),
                Transaction(
                    statement_id=statement.id,
                    transaction_date=current,
                    posting_date=current,
                    amount_minor=-2_000,
                    currency="CNY",
                    raw_description="商户退款",
                ),
                Transaction(
                    statement_id=statement.id,
                    transaction_date=current,
                    posting_date=current,
                    amount_minor=360_000,
                    currency="CNY",
                    raw_description="主卡年费",
                ),
                Transaction(
                    statement_id=statement.id,
                    transaction_date=current,
                    posting_date=current,
                    amount_minor=97_526,
                    currency="CNY",
                    raw_description="还款失败 败－其他",
                ),
                Transaction(
                    statement_id=statement.id,
                    transaction_date=current,
                    posting_date=current,
                    amount_minor=-10_000,
                    currency="CNY",
                    raw_description="信用卡还款",
                ),
            ]
        )
        session.commit()

    response = client.get("/api/v1/analytics?months=1", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    curr_consumption = payload["monthly_consumption"][0]

    assert curr_consumption["gross_purchase_minor"] == 15_000
    assert curr_consumption["refund_minor"] == 2_000
    assert curr_consumption["net_consumption_minor"] == 13_000
    assert curr_consumption["amount_minor"] == 13_000
    assert curr_consumption["transaction_count"] == 2


def test_analytics_preserves_signed_cross_month_refund_activity(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    today = date(2026, 9, 15)
    august = _month_start(today, -1)
    september = _month_start(today)

    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="跨月退款测试卡",
            last4="4321",
            currency="CNY",
        )
        purchase_statement = Statement(
            bank_code=card.bank_code,
            card=card,
            statement_date=august,
            due_date=september,
            total_due_minor=10_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        refund_statement = Statement(
            bank_code="CMB",
            card=card,
            statement_date=september,
            due_date=_month_start(today, 1),
            total_due_minor=7_777,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        purchase = Transaction(
            statement=purchase_statement,
            transaction_date=august,
            posting_date=august,
            amount_minor=10_000,
            currency="CNY",
            raw_description="跨月退款原消费",
            transaction_type="purchase",
        )
        refund = Transaction(
            statement=refund_statement,
            transaction_date=september,
            posting_date=september,
            amount_minor=-10_000,
            currency="CNY",
            raw_description="跨月全额退款",
            transaction_type="refund",
            original_transaction=purchase,
        )
        session.add_all([card, purchase_statement, refund_statement, purchase, refund])
        session.commit()
        refund_statement_id = refund_statement.id

    response = client.get("/api/v1/analytics?months=2", headers=auth_headers)
    assert response.status_code == 200, response.text
    monthly = response.json()["monthly_consumption"]

    assert monthly[0]["month"] == august.strftime("%Y-%m")
    assert monthly[0]["net_consumption_minor"] == 10_000
    assert monthly[0]["amount_minor"] == 10_000
    assert monthly[1]["month"] == september.strftime("%Y-%m")
    assert monthly[1]["gross_purchase_minor"] == 0
    assert monthly[1]["refund_minor"] == 10_000
    assert monthly[1]["net_consumption_minor"] == -10_000
    assert monthly[1]["amount_minor"] == -10_000

    with client.app.state.session_factory() as session:
        refund_statement = session.get(Statement, refund_statement_id)
        assert refund_statement.total_due_minor == 7_777
        session.add(
            Transaction(
                statement=refund_statement,
                transaction_date=september,
                posting_date=september,
                amount_minor=5_000,
                currency="CNY",
                raw_description="退款月消费",
                transaction_type="purchase",
            )
        )
        session.commit()

    response = client.get("/api/v1/analytics?months=2", headers=auth_headers)
    assert response.status_code == 200, response.text
    september_activity = response.json()["monthly_consumption"][1]
    assert september_activity["gross_purchase_minor"] == 5_000
    assert september_activity["refund_minor"] == 10_000
    assert september_activity["net_consumption_minor"] == -5_000
    assert september_activity["amount_minor"] == -5_000
