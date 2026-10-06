from datetime import date, timedelta
from types import SimpleNamespace

from app.models.card import Card
from app.models.statement import ParseStatus, Statement
from app.models.transaction import Transaction
from app.modules.core.transaction import (
    CLASSIFICATION_RULE_VERSION,
    ClassificationSource,
    TransactionType,
    apply_rule_classification,
    classify_transaction,
)
from app.modules.optional.interest_free import (
    DueDateRuleType,
    EstimationLevel,
    forecast_interest_free_days,
)
from app.services.financial_analysis.queries import list_calendar_statements
from app.services.statement_date_semantics import logical_rule_anchor


def test_rule_classification_has_provenance() -> None:
    result = classify_transaction(12000, "普通消费")
    assert result.transaction_type is TransactionType.PURCHASE
    assert result.source is ClassificationSource.RULE
    assert result.rule_version == CLASSIFICATION_RULE_VERSION


def test_rule_reclassification_preserves_manual_override() -> None:
    transaction = SimpleNamespace(
        amount_minor=-2000,
        raw_description="退款",
        transaction_type=TransactionType.REWARD.value,
        classification_source=ClassificationSource.MANUAL.value,
        classification_rule_version=None,
    )

    result = apply_rule_classification(transaction)

    assert result is TransactionType.REWARD
    assert transaction.transaction_type == TransactionType.REWARD.value
    assert transaction.classification_source == ClassificationSource.MANUAL.value
    assert transaction.classification_rule_version is None


def test_owner_semantic_correction_is_auditable(client, auth_headers) -> None:
    today = date.today()
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="TST",
            bank_name="Test Bank",
            display_name="Semantic Card",
            last4="1001",
            currency="CNY",
        )
        statement = Statement(bank_code="CMB",
            card=card,
            statement_date=today,
            due_date=today + timedelta(days=20),
            total_due_minor=1000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        transaction = Transaction(
            statement=statement,
            transaction_date=today,
            posting_date=today,
            amount_minor=-1000,
            currency="CNY",
            raw_description="Statement credit",
            transaction_type=TransactionType.UNKNOWN.value,
            classification_source=ClassificationSource.RULE.value,
            classification_rule_version=CLASSIFICATION_RULE_VERSION,
        )
        session.add_all([card, statement, transaction])
        session.commit()
        transaction_id = transaction.id

    response = client.patch(
        f"/api/v1/transactions/{transaction_id}/semantics",
        headers=auth_headers,
        json={"transaction_type": "reward"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["transaction_type"] == "reward"
    assert payload["classification_source"] == "manual"
    assert payload["classification_rule_version"] is None


def test_owner_semantic_correction_cannot_break_refund_relation(client, auth_headers) -> None:
    today = date.today()
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="TST",
            bank_name="Test Bank",
            display_name="Relation Card",
            last4="1005",
            currency="CNY",
        )
        statement = Statement(bank_code="CMB",
            card=card,
            statement_date=today,
            due_date=today + timedelta(days=20),
            total_due_minor=10_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        purchase = Transaction(
            statement=statement,
            transaction_date=today,
            posting_date=today,
            amount_minor=10_000,
            currency="CNY",
            raw_description="原消费",
            transaction_type=TransactionType.PURCHASE.value,
        )
        refund = Transaction(
            statement=statement,
            transaction_date=today,
            posting_date=today,
            amount_minor=-2_000,
            currency="CNY",
            raw_description="部分退款",
            transaction_type=TransactionType.REFUND.value,
            original_transaction=purchase,
        )
        session.add_all([card, statement, purchase, refund])
        session.commit()
        purchase_id = purchase.id
        refund_id = refund.id

    purchase_response = client.patch(
        f"/api/v1/transactions/{purchase_id}/semantics",
        headers=auth_headers,
        json={"transaction_type": "fee"},
    )
    assert purchase_response.status_code == 409

    refund_response = client.patch(
        f"/api/v1/transactions/{refund_id}/semantics",
        headers=auth_headers,
        json={"transaction_type": "reward"},
    )
    assert refund_response.status_code == 409


def test_observed_repayment_does_not_reduce_statement_remaining(client, auth_headers) -> None:
    today = date.today()
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="TST",
            bank_name="Test Bank",
            display_name="Repayment Card",
            last4="1002",
            currency="CNY",
        )
        statement = Statement(bank_code="CMB",
            card=card,
            statement_date=today,
            due_date=today + timedelta(days=20),
            total_due_minor=10_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        observed = Transaction(
            statement=statement,
            transaction_date=today,
            posting_date=today,
            amount_minor=-10_000,
            currency="CNY",
            raw_description="信用卡还款",
            transaction_type=TransactionType.REPAYMENT.value,
            classification_source=ClassificationSource.RULE.value,
            classification_rule_version=CLASSIFICATION_RULE_VERSION,
        )
        session.add_all([card, statement, observed])
        session.commit()

    due = client.get("/api/v1/due", headers=auth_headers)
    assert due.status_code == 200, due.text
    assert due.json()["total_remaining_minor"] == 10_000


def test_analytics_and_dashboard_share_historical_outstanding_scope(client, auth_headers) -> None:
    today = date.today()
    current_month = date(today.year, today.month, 1)
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="TST",
            bank_name="Test Bank",
            display_name="Historical Card",
            last4="1003",
            currency="CNY",
            credit_limit_minor=100_000,
        )
        historical = Statement(bank_code="CMB",
            card=card,
            statement_date=current_month,
            due_date=today - timedelta(days=10),
            total_due_minor=50_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        session.add_all([card, historical])
        session.commit()

    dashboard = client.get("/api/v1/dashboard", headers=auth_headers)
    analytics = client.get("/api/v1/analytics?months=1", headers=auth_headers)
    assert dashboard.status_code == 200, dashboard.text
    assert analytics.status_code == 200, analytics.text
    assert dashboard.json()["due"]["total_remaining_minor"] == 0
    assert analytics.json()["bank_utilization"][0]["outstanding_minor"] == 0
    assert analytics.json()["card_utilization"][0]["outstanding_minor"] == 0


def test_analytics_exposes_partial_coverage_instead_of_confirmed_zero(client, auth_headers) -> None:
    today = date.today()
    current_month = date(today.year, today.month, 1)
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="TST",
            bank_name="Test Bank",
            display_name="Partial Card",
            last4="1004",
            currency="CNY",
        )
        statement = Statement(bank_code="CMB",
            card=card,
            statement_date=current_month,
            due_date=today + timedelta(days=20),
            total_due_minor=0,
            currency="CNY",
            parse_status=ParseStatus.TRANSACTIONS_PARTIAL,
        )
        session.add_all([card, statement])
        session.commit()

    analytics = client.get("/api/v1/analytics?months=1", headers=auth_headers)
    assert analytics.status_code == 200, analytics.text
    payload = analytics.json()
    assert payload["monthly_consumption"][0]["amount_minor"] == 0
    assert payload["coverage"]["status"] == "partial"
    assert payload["coverage"]["partial_statement_count"] == 1


def test_analytics_coverage_reports_undated_transaction(client, auth_headers) -> None:
    today = date.today()
    current_month = date(today.year, today.month, 1)
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="TST",
            bank_name="Test Bank",
            display_name="Undated Card",
            last4="1006",
            currency="CNY",
        )
        statement = Statement(bank_code="CMB",
            card=card,
            statement_date=current_month,
            due_date=today + timedelta(days=20),
            total_due_minor=1_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        transaction = Transaction(
            statement=statement,
            transaction_date=None,
            posting_date=None,
            amount_minor=1_000,
            currency="CNY",
            raw_description="无日期消费",
            transaction_type=TransactionType.PURCHASE.value,
        )
        session.add_all([card, statement, transaction])
        session.commit()

    analytics = client.get("/api/v1/analytics?months=1", headers=auth_headers)
    assert analytics.status_code == 200, analytics.text
    coverage = analytics.json()["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["undated_transaction_count"] == 1


def test_interest_free_analysis_is_available_through_engine_boundary() -> None:
    result = forecast_interest_free_days(
        card_id="card-1",
        spend_date=date(2026, 9, 9),
        statement_day=9,
        due_date_rule_type=DueDateRuleType.DAYS_AFTER_STATEMENT,
        due_date_rule_value=18,
    )
    assert result.estimation_level is EstimationLevel.FULLY_ESTIMATED
    assert result.conservative_interest_free_days == 18
    assert result.conservative is True


def test_icbc_logical_rule_anchor_does_not_shift_monthly_statement_record(client) -> None:
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="ICBC",
            bank_name="中国工商银行",
            display_name="ICBC natural month",
            last4="9002",
            statement_day=1,
            statement_day_boundary="next_cycle",
            due_day=25,
            due_date_rule_type="fixed_day",
            due_date_rule_value=25,
        )
        statement = Statement(bank_code="CMB",
            card=card,
            statement_date=date(2025, 9, 30),
            due_date=date(2025, 10, 25),
            total_due_minor=10_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        session.add_all([card, statement])
        session.commit()

        september = list_calendar_statements(
            session,
            start_date=date(2025, 9, 1),
            end_date=date(2025, 9, 30),
        )
        october = list_calendar_statements(
            session,
            start_date=date(2025, 10, 1),
            end_date=date(2025, 10, 31),
        )

        assert [item.id for item in september] == [statement.id]
        assert october == []
        assert statement.statement_date == date(2025, 9, 30)
        assert logical_rule_anchor(
            statement.statement_date, bank_code="ICBC", statement_day=1
        ) == date(2025, 10, 1)
