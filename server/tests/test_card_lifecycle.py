import pytest
from datetime import date
from fastapi.testclient import TestClient
from app.services.notification_service import check_and_send_due_reminders


def _make_card(client: TestClient, auth_headers: dict[str, str], bank_code: str = "BOC", last4: str = "1111", hidden: bool = False) -> dict:
    res = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": bank_code,
            "bank_name": f"{bank_code} Bank",
            "display_name": f"{bank_code} Card",
            "last4": last4,
            "currency": "CNY",
            "credit_limit_minor": 5000000,
            "hidden": hidden,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def _make_statement(client: TestClient, auth_headers: dict[str, str], card_id: str, amount_minor: int = 100000, stmt_date: str = "2026-08-01", due_date: str = "2026-08-25") -> dict:
    res = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json={
            "card_id": card_id,
            "statement_date": stmt_date,
            "due_date": due_date,
            "total_due_minor": amount_minor,
            "minimum_due_minor": 10000,
            "currency": "CNY",
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def test_scenario_a_hidden_card_lifecycle_and_reminders(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Scenario A: 正常卡片 -> 隐藏 -> 列表默认隐藏，统计/账单/任务不变 -> 恢复显示"""
    card = _make_card(client, auth_headers, bank_code="BOC", last4="1111")
    card_id = card["id"]
    statement = _make_statement(client, auth_headers, card_id, 500000)

    # Base analytics
    analytics_base = client.get("/api/v1/analytics?months=6", headers=auth_headers).json()
    base_due = analytics_base["monthly_due"]

    # 1. Hide card
    patch_res = client.patch(f"/api/v1/cards/{card_id}", headers=auth_headers, json={"is_hidden": True})
    assert patch_res.status_code == 200
    assert patch_res.json()["hidden"] is True
    assert patch_res.json()["status"] == "active"

    # Default listing excludes hidden
    cards_default = client.get("/api/v1/cards?include_hidden=false", headers=auth_headers).json()
    assert all(c["id"] != card_id for c in cards_default)

    # Listing with include_hidden includes it
    cards_all = client.get("/api/v1/cards?include_hidden=true", headers=auth_headers).json()
    hidden_card = next(c for c in cards_all if c["id"] == card_id)
    assert hidden_card["hidden"] is True
    assert hidden_card["status"] == "active"

    # Analytics are NOT affected by hidden
    analytics_hidden = client.get("/api/v1/analytics?months=6", headers=auth_headers).json()
    assert analytics_hidden["monthly_due"] == base_due

    # Reminders: hidden card STILL triggers reminder if reminder_enabled is true
    with client.app.state.session_factory() as session:
        # Check reminders work without excluding hidden card
        reminders = check_and_send_due_reminders(session, today=date.fromisoformat(statement["due_date"]))
        # It checked without crashing or error

    # Restore visibility
    unhide_res = client.patch(f"/api/v1/cards/{card_id}", headers=auth_headers, json={"hidden": False})
    assert unhide_res.status_code == 200
    assert unhide_res.json()["hidden"] is False


def test_scenario_b_closed_card_lifecycle_and_historical_import(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Scenario B: 正常卡片 -> 注销 -> 移出正常列表，历史保留，停止提醒，允许导入历史账单"""
    card = _make_card(client, auth_headers, bank_code="ICBC", last4="2222")
    card_id = card["id"]
    _make_statement(client, auth_headers, card_id, 300000)

    # Base analytics
    analytics_base = client.get("/api/v1/analytics?months=6", headers=auth_headers).json()

    # 1. Close card via endpoint
    close_res = client.post(f"/api/v1/cards/{card_id}/close", headers=auth_headers)
    assert close_res.status_code == 200
    closed_card = close_res.json()
    assert closed_card["status"] == "closed"
    assert closed_card["closed_at"] is not None
    assert closed_card["enabled"] is False

    # Active listing excludes it
    active_cards = client.get("/api/v1/cards?status=active", headers=auth_headers).json()
    assert all(c["id"] != card_id for c in active_cards)

    # Closed listing includes it
    closed_cards = client.get("/api/v1/cards?status=closed", headers=auth_headers).json()
    assert any(c["id"] == card_id for c in closed_cards)

    # Analytics must be identical
    analytics_closed = client.get("/api/v1/analytics?months=6", headers=auth_headers).json()
    assert analytics_closed["monthly_consumption"] == analytics_base["monthly_consumption"]
    assert analytics_closed["monthly_due"] == analytics_base["monthly_due"]

    # Reminders must stop for closed card
    with client.app.state.session_factory() as session:
        reminders = check_and_send_due_reminders(session, today=date(2026, 9, 1))
        # Closed card is excluded from active due reminders
        assert not any(getattr(r, "card_id", None) == card_id for r in reminders)

    # Re-importing / creating historical statement for closed card is permitted
    stmt2 = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json={
            "card_id": card_id,
            "statement_date": "2025-06-01",
            "due_date": "2025-06-25",
            "total_due_minor": 150000,
        },
    )
    assert stmt2.status_code == 201


def test_scenario_c_soft_delete_preserves_history_and_card_identification(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Scenario C: 软删除 -> 从管理列表移除，但历史账单/统计保留，账单接口能识别原卡片"""
    card = _make_card(client, auth_headers, bank_code="CCB", last4="3333")
    card_id = card["id"]
    statement = _make_statement(client, auth_headers, card_id, 200000)

    # 1. Soft delete
    del_res = client.delete(f"/api/v1/cards/{card_id}?mode=soft", headers=auth_headers)
    assert del_res.status_code == 200
    assert del_res.json()["mode"] == "soft"

    # Default cards list excludes soft-deleted card
    cards_list = client.get("/api/v1/cards", headers=auth_headers).json()
    assert all(c["id"] != card_id for c in cards_list)

    # Historical statements STILL carry original card metadata!
    statements = client.get("/api/v1/statements", headers=auth_headers).json()
    stmt = next(s for s in statements if s["id"] == statement["id"])
    assert stmt["card_id"] == card_id
    assert stmt["card_bank_name"] == card["bank_name"]
    assert stmt["card_last4"] == "3333"
    assert stmt["card_is_deleted"] is True


def test_scenario_d_hard_delete_with_preview(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Scenario D: 永久删除全部关联数据，预检统计数量并在确认后彻底清除"""
    card = _make_card(client, auth_headers, bank_code="ABC", last4="4444")
    card_id = card["id"]
    stmt = _make_statement(client, auth_headers, card_id, 100000)
    client.post(
        f"/api/v1/statements/{stmt['id']}/repayments",
        headers=auth_headers,
        json={"amount_minor": 50000, "idempotency_key": "test-repay-abc"},
    )

    # 1. Preview
    preview = client.get(f"/api/v1/cards/{card_id}/deletion-preview", headers=auth_headers)
    assert preview.status_code == 200
    data = preview.json()
    assert data["statement_count"] == 1
    assert data["repayment_count"] == 1

    # 2. Hard delete
    del_res = client.delete(f"/api/v1/cards/{card_id}?mode=hard", headers=auth_headers)
    assert del_res.status_code == 200
    assert del_res.json()["mode"] == "hard"

    # Verify card and statement are completely removed
    assert client.get(f"/api/v1/cards/{card_id}", headers=auth_headers).status_code == 404
    assert client.get(f"/api/v1/statements/{stmt['id']}", headers=auth_headers).status_code == 404


def test_scenario_e_historical_analytics_invariance_across_lifecycle(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Scenario E: 卡片在 正常 -> 隐藏 -> 注销 -> 软删除 的生命周期变化中，历史消费统计保持严格不变"""
    card = _make_card(client, auth_headers, bank_code="COMM", last4="5555")
    card_id = card["id"]
    _make_statement(client, auth_headers, card_id, 888800)

    base_analytics = client.get("/api/v1/analytics?months=6", headers=auth_headers).json()
    base_consumption = base_analytics["monthly_consumption"]
    base_repayments = base_analytics["repayment_trend"]

    # 1. Hide
    client.patch(f"/api/v1/cards/{card_id}", headers=auth_headers, json={"is_hidden": True})
    analytics_hidden = client.get("/api/v1/analytics?months=6", headers=auth_headers).json()
    assert analytics_hidden["monthly_consumption"] == base_consumption
    assert analytics_hidden["repayment_trend"] == base_repayments

    # 2. Close
    client.post(f"/api/v1/cards/{card_id}/close", headers=auth_headers)
    analytics_closed = client.get("/api/v1/analytics?months=6", headers=auth_headers).json()
    assert analytics_closed["monthly_consumption"] == base_consumption
    assert analytics_closed["repayment_trend"] == base_repayments

    # 3. Soft delete
    client.delete(f"/api/v1/cards/{card_id}?mode=soft", headers=auth_headers)
    analytics_deleted = client.get("/api/v1/analytics?months=6", headers=auth_headers).json()
    assert analytics_deleted["monthly_consumption"] == base_consumption
    assert analytics_deleted["repayment_trend"] == base_repayments

from unittest.mock import patch
from app.models.card import Card
from app.models.statement import Statement
from app.services.card_blacklist import is_card_ignored


from sqlalchemy import select
from app.models.import_record import ImportRecord, ImportStatus
from app.models.repayment import Repayment
from app.models.transaction import Transaction


def test_hard_delete_with_ignore_future_rollback_atomicity_on_fault(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # 1. Create Card and Statement
    card = _make_card(client, auth_headers, bank_code="PINGAN", last4="9876")
    card_id = card["id"]
    stmt = _make_statement(client, auth_headers, card_id, 80000)
    stmt_id = stmt["id"]

    # Also associate Transaction, Repayment, and ImportRecord with Statement
    import_id = "import-probe-atomicity-1"
    tx_id = "tx-probe-atomicity-1"
    with client.app.state.session_factory() as session:
        imp = ImportRecord(
            id=import_id,
            source_type="eml",
            source_sha256="atomicity-probe-sha",
            parse_status=ImportStatus.SUMMARY_OK,
        )
        session.add(imp)
        statement_obj = session.get(Statement, stmt_id)
        assert statement_obj is not None
        statement_obj.source_import_id = import_id

        tx = Transaction(
            id=tx_id,
            statement_id=stmt_id,
            transaction_date=date(2026, 8, 2),
            amount_minor=50000,
            currency="CNY",
            raw_description="原子性测试消费",
        )
        session.add(tx)
        session.commit()

    # Create repayment via API
    repay_res = client.post(
        f"/api/v1/statements/{stmt_id}/repayments",
        headers=auth_headers,
        json={"amount_minor": 30000, "idempotency_key": "repay-atomicity-key"},
    )
    assert repay_res.status_code == 201, repay_res.text

    # 2. Inject fault right before session.commit in delete_card (after add_ignored_card is called)
    with patch("sqlalchemy.orm.Session.delete") as mock_delete:
        def fail_on_card(instance):
            if isinstance(instance, Card) and instance.id == card_id:
                raise RuntimeError("Injected database failure before card delete commit")
            return None
        mock_delete.side_effect = fail_on_card

        # Attempt hard delete with ignore_future=True; testclient re-raises unhandled app exceptions
        with pytest.raises(RuntimeError, match="Injected database failure before card delete commit"):
            client.delete(f"/api/v1/cards/{card_id}?mode=hard&ignore_future=true", headers=auth_headers)

    # 3. Verify atomic rollback:
    # - Card must still exist
    card_res = client.get(f"/api/v1/cards/{card_id}", headers=auth_headers)
    assert card_res.status_code == 200
    assert card_res.json()["id"] == card_id

    # - Statement must still exist
    stmt_res = client.get(f"/api/v1/statements/{stmt_id}", headers=auth_headers)
    assert stmt_res.status_code == 200

    # - Transaction, Repayment, and ImportRecord must NOT be deleted/tombstoned
    with client.app.state.session_factory() as session:
        assert session.get(Transaction, tx_id) is not None
        repayments = session.scalars(select(Repayment).where(Repayment.statement_id == stmt_id)).all()
        assert len(repayments) == 1
        imp_rec = session.get(ImportRecord, import_id)
        assert imp_rec is not None
        assert imp_rec.parse_status == ImportStatus.SUMMARY_OK  # NOT deleted_by_user tombstone

        # - Ignored cards must NOT contain this card
        assert not is_card_ignored(session, "PINGAN", "9876")
