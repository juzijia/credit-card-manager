from datetime import date, timedelta
from fastapi.testclient import TestClient

from app.core.plain_secrets import set_secret
from app.models.card import Card
from app.models.statement import ParseStatus, Statement
from app.models.transaction import Transaction
from app.modules.core.transaction import (
    CLASSIFICATION_RULE_VERSION,
    ClassificationSource,
    TransactionType,
)


def _setup_card(session, code="CMB", last4="1234") -> Card:
    card = Card(
        bank_code=code,
        bank_name="Test Bank",
        display_name=f"Test Card {last4}",
        last4=last4,
        currency="CNY",
    )
    session.add(card)
    session.flush()
    return card


def test_statement_analysis_authority_and_semantic_separation(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    today = date(2026, 9, 1)
    with client.app.state.session_factory() as session:
        card = _setup_card(session, "BOC", "8888")
        statement = Statement(
            bank_code="CMB",
            card=card,
            statement_date=today,
            due_date=today + timedelta(days=20),
            total_due_minor=500_000,
            minimum_due_minor=50_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        session.add(statement)
        session.flush()

        txs = [
            Transaction(
                statement=statement,
                transaction_date=today,
                posting_date=today,
                amount_minor=100_000,
                currency="CNY",
                raw_description="普通消费A",
                transaction_type=TransactionType.PURCHASE.value,
                classification_source=ClassificationSource.RULE.value,
            ),
            Transaction(
                statement=statement,
                transaction_date=today,
                posting_date=today,
                amount_minor=50_000,
                currency="CNY",
                raw_description="普通消费B",
                transaction_type=TransactionType.PURCHASE.value,
                classification_source=ClassificationSource.RULE.value,
            ),
            Transaction(
                statement=statement,
                transaction_date=today,
                posting_date=today,
                amount_minor=-30_000,
                currency="CNY",
                raw_description="退款",
                transaction_type=TransactionType.REFUND.value,
                classification_source=ClassificationSource.RULE.value,
            ),
            Transaction(
                statement=statement,
                transaction_date=today,
                posting_date=today,
                amount_minor=10_000,
                currency="CNY",
                raw_description="分期手续费",
                transaction_type=TransactionType.FEE.value,
                classification_source=ClassificationSource.RULE.value,
            ),
            Transaction(
                statement=statement,
                transaction_date=today,
                posting_date=today,
                amount_minor=5_000,
                currency="CNY",
                raw_description="循环利息",
                transaction_type=TransactionType.INTEREST.value,
                classification_source=ClassificationSource.RULE.value,
            ),
            Transaction(
                statement=statement,
                transaction_date=today,
                posting_date=today,
                amount_minor=48_000,
                currency="CNY",
                raw_description="年费",
                transaction_type=TransactionType.ANNUAL_FEE.value,
                classification_source=ClassificationSource.RULE.value,
            ),
            Transaction(
                statement=statement,
                transaction_date=today,
                posting_date=today,
                amount_minor=-200_000,
                currency="CNY",
                raw_description="信用卡还款",
                transaction_type=TransactionType.REPAYMENT.value,
                classification_source=ClassificationSource.RULE.value,
            ),
            Transaction(
                statement=statement,
                transaction_date=today,
                posting_date=today,
                amount_minor=7_777,
                currency="CNY",
                raw_description="未知交易",
                transaction_type=TransactionType.UNKNOWN.value,
                classification_source=ClassificationSource.RULE.value,
                classification_rule_version=CLASSIFICATION_RULE_VERSION,
            ),
        ]
        session.add_all(txs)
        session.commit()
        stmt_id = statement.id

    # 1. Check statement fact endpoint
    resp_stmt = client.get(f"/api/v1/statements/{stmt_id}", headers=auth_headers)
    assert resp_stmt.status_code == 200
    stmt_data = resp_stmt.json()
    assert stmt_data["total_due_minor"] == 500_000
    assert stmt_data["remaining_minor"] == 500_000

    # 2. Check statement analysis endpoint
    resp_analysis = client.get(f"/api/v1/statements/{stmt_id}/analysis", headers=auth_headers)
    assert resp_analysis.status_code == 200
    analysis = resp_analysis.json()

    assert analysis["statement_id"] == stmt_id
    m = analysis["metrics"]
    assert m["gross_purchase_minor"] == 150_000
    assert m["purchase_count"] == 2
    assert m["refund_observed_minor"] == 30_000
    assert m["period_net_consumption_minor"] == 120_000
    assert m["fee_activity_minor"] == 10_000
    assert m["interest_activity_minor"] == 5_000
    assert m["annual_fee_activity_minor"] == 48_000
    assert m["observed_repayment_activity_minor"] == -200_000
    assert m["unknown_activity_minor"] == 7_777

    c = analysis["coverage"]
    assert c["status"] == "partial"
    assert c["unknown_transaction_count"] == 1
    assert c["undated_transaction_count"] == 0

    # 3. Statement fact endpoint must be unchanged
    resp_stmt_after = client.get(f"/api/v1/statements/{stmt_id}", headers=auth_headers)
    assert resp_stmt_after.json()["total_due_minor"] == 500_000
    assert resp_stmt_after.json()["remaining_minor"] == 500_000


def test_statement_analysis_coverage_variants(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    today = date(2026, 9, 1)
    with client.app.state.session_factory() as session:
        card = _setup_card(session, "ICBC", "9999")

        s_complete = Statement(
            bank_code="CMB",
            card=card,
            statement_date=today,
            due_date=today + timedelta(days=20),
            total_due_minor=10_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        s_undated = Statement(
            bank_code="CMB",
            card=card,
            statement_date=today - timedelta(days=30),
            due_date=today - timedelta(days=10),
            total_due_minor=20_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        s_partial = Statement(
            bank_code="CMB",
            card=card,
            statement_date=today - timedelta(days=60),
            due_date=today - timedelta(days=40),
            total_due_minor=30_000,
            currency="CNY",
            parse_status=ParseStatus.SUMMARY_OK,
        )
        s_failed = Statement(
            bank_code="CMB",
            card=card,
            statement_date=today - timedelta(days=90),
            due_date=today - timedelta(days=70),
            total_due_minor=0,
            currency="CNY",
            parse_status=ParseStatus.FAILED,
        )
        session.add_all([s_complete, s_undated, s_partial, s_failed])
        session.flush()

        tx_complete = Transaction(
            statement=s_complete,
            transaction_date=today,
            posting_date=today,
            amount_minor=10_000,
            currency="CNY",
            raw_description="全信息消费",
            transaction_type=TransactionType.PURCHASE.value,
        )
        tx_undated = Transaction(
            statement=s_undated,
            transaction_date=None,
            posting_date=None,
            amount_minor=20_000,
            currency="CNY",
            raw_description="无日期流水",
            transaction_type=TransactionType.PURCHASE.value,
        )
        session.add_all([tx_complete, tx_undated])
        session.commit()

        id_complete = s_complete.id
        id_undated = s_undated.id
        id_partial = s_partial.id
        id_failed = s_failed.id

    res_c = client.get(f"/api/v1/statements/{id_complete}/analysis", headers=auth_headers).json()
    assert res_c["coverage"]["status"] == "complete"
    assert res_c["coverage"]["unknown_transaction_count"] == 0
    assert res_c["coverage"]["undated_transaction_count"] == 0

    res_u = client.get(f"/api/v1/statements/{id_undated}/analysis", headers=auth_headers).json()
    assert res_u["coverage"]["status"] == "partial"
    assert res_u["coverage"]["undated_transaction_count"] == 1

    res_p = client.get(f"/api/v1/statements/{id_partial}/analysis", headers=auth_headers).json()
    assert res_p["coverage"]["status"] == "partial"
    assert res_p["metrics"]["gross_purchase_minor"] == 0
    assert res_p["coverage"]["status"] != "complete"

    res_f = client.get(f"/api/v1/statements/{id_failed}/analysis", headers=auth_headers).json()
    assert res_f["coverage"]["status"] == "missing"


def test_statement_analysis_manual_correction_recompute(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    today = date(2026, 9, 1)
    with client.app.state.session_factory() as session:
        card = _setup_card(session, "CCB", "6666")
        statement = Statement(
            bank_code="CMB",
            card=card,
            statement_date=today,
            due_date=today + timedelta(days=20),
            total_due_minor=20_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        session.add(statement)
        session.flush()

        tx1 = Transaction(
            statement=statement,
            transaction_date=today,
            posting_date=today,
            amount_minor=15_000,
            currency="CNY",
            raw_description="普通消费",
            transaction_type=TransactionType.PURCHASE.value,
            classification_source=ClassificationSource.RULE.value,
            classification_rule_version=CLASSIFICATION_RULE_VERSION,
        )
        tx_unknown = Transaction(
            statement=statement,
            transaction_date=today,
            posting_date=today,
            amount_minor=5_000,
            currency="CNY",
            raw_description="模糊商户扣费",
            transaction_type=TransactionType.UNKNOWN.value,
            classification_source=ClassificationSource.RULE.value,
            classification_rule_version=CLASSIFICATION_RULE_VERSION,
        )
        session.add_all([tx1, tx_unknown])
        session.commit()
        stmt_id = statement.id
        tx_unknown_id = tx_unknown.id

    # 1. Initial analysis
    init_res = client.get(f"/api/v1/statements/{stmt_id}/analysis", headers=auth_headers)
    assert init_res.status_code == 200
    init_data = init_res.json()
    assert init_data["metrics"]["gross_purchase_minor"] == 15_000
    assert init_data["metrics"]["unknown_activity_minor"] == 5_000
    assert init_data["coverage"]["status"] == "partial"
    assert init_data["coverage"]["unknown_transaction_count"] == 1

    # 2. Owner corrects unknown transaction to FEE
    patch_res = client.patch(
        f"/api/v1/transactions/{tx_unknown_id}/semantics",
        headers=auth_headers,
        json={"transaction_type": "fee"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["transaction_type"] == "fee"
    assert patch_res.json()["classification_source"] == "manual"

    # 3. Query analysis again: metrics dynamically updated, coverage becomes complete
    recomputed_res = client.get(f"/api/v1/statements/{stmt_id}/analysis", headers=auth_headers)
    assert recomputed_res.status_code == 200
    recomputed_data = recomputed_res.json()
    m = recomputed_data["metrics"]
    assert m["gross_purchase_minor"] == 15_000
    assert m["fee_activity_minor"] == 5_000
    assert m["unknown_activity_minor"] == 0
    assert recomputed_data["coverage"]["status"] == "complete"
    assert recomputed_data["coverage"]["unknown_transaction_count"] == 0

    # 4. Confirm Statement facts did not change
    stmt_check = client.get(f"/api/v1/statements/{stmt_id}", headers=auth_headers).json()
    assert stmt_check["total_due_minor"] == 20_000
    assert stmt_check["remaining_minor"] == 20_000


def test_statement_analysis_strict_statement_membership_isolation(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    today = date(2026, 9, 1)
    with client.app.state.session_factory() as session:
        card = _setup_card(session, "ABC", "1111")
        stmt_a = Statement(
            bank_code="CMB",
            card=card,
            statement_date=today,
            due_date=today + timedelta(days=20),
            total_due_minor=10_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        stmt_b = Statement(
            bank_code="CMB",
            card=card,
            statement_date=today - timedelta(days=30),
            due_date=today - timedelta(days=10),
            total_due_minor=50_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        session.add_all([stmt_a, stmt_b])
        session.flush()

        tx_a = Transaction(
            statement=stmt_a,
            transaction_date=today,
            posting_date=today,
            amount_minor=10_000,
            currency="CNY",
            raw_description="账单A消费",
            transaction_type=TransactionType.PURCHASE.value,
        )
        tx_b = Transaction(
            statement=stmt_b,
            transaction_date=today - timedelta(days=30),
            posting_date=today - timedelta(days=30),
            amount_minor=50_000,
            currency="CNY",
            raw_description="账单B消费",
            transaction_type=TransactionType.PURCHASE.value,
        )
        session.add_all([tx_a, tx_b])
        session.commit()
        stmt_a_id = stmt_a.id
        stmt_b_id = stmt_b.id

    res_a = client.get(f"/api/v1/statements/{stmt_a_id}/analysis", headers=auth_headers).json()
    assert res_a["metrics"]["gross_purchase_minor"] == 10_000
    assert res_a["metrics"]["purchase_count"] == 1

    res_b = client.get(f"/api/v1/statements/{stmt_b_id}/analysis", headers=auth_headers).json()
    assert res_b["metrics"]["gross_purchase_minor"] == 50_000
    assert res_b["metrics"]["purchase_count"] == 1


def test_statement_analysis_agent_authorization(client: TestClient) -> None:
    """Verify that trusted Agent Token can access statement analysis (read-only)."""
    today = date(2026, 9, 1)
    agent_token_raw = "agent-test-token-123456789012345678901234"
    set_secret("agent", "token", agent_token_raw)

    with client.app.state.session_factory() as session:
        card = _setup_card(session, "BOC", "3333")
        statement = Statement(
            bank_code="CMB",
            card=card,
            statement_date=today,
            due_date=today + timedelta(days=20),
            total_due_minor=10_000,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        session.add_all([card, statement])
        session.commit()
        stmt_id = statement.id

    agent_headers = {"Authorization": f"Bearer {agent_token_raw}"}
    resp = client.get(f"/api/v1/statements/{stmt_id}/analysis", headers=agent_headers)
    assert resp.status_code == 200
    assert resp.json()["statement_id"] == stmt_id
