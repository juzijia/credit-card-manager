from datetime import date, timedelta
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.models.card import Card
from app.models.notification import NotificationSetting
from app.models.statement import Statement
from app.models.system_setting import SystemSetting
from app.services.notification_service import check_and_send_due_reminders
from app.services.system_settings import (
    ensure_history_cutoff_date,
    get_history_cutoff_date,
    set_system_setting,
)


def _create_card(client: TestClient, auth_headers: dict[str, str]) -> dict:
    res = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "招商经典白",
            "currency": "CNY",
            "last4": "8888",
            "credit_limit_yuan": "50000",
            "billing_cycle_type": "statement_due_fixed",
            "statement_day": 5,
            "due_day": 25,
            "annual_fee_cny": 3600,
            "annual_fee_policy": "10000积分兑换",
            "card_product": "经典白金卡",
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def _create_stmt(
    client: TestClient,
    auth_headers: dict[str, str],
    card_id: str,
    stmt_date: str,
    due_date: str,
    amount_minor: int = 100000,
) -> dict:
    res = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json={
            "card_id": card_id,
            "statement_date": stmt_date,
            "due_date": due_date,
            "total_due_minor": amount_minor,
            "minimum_due_minor": amount_minor // 10,
            "currency": "CNY",
            "parse_status": "summary_ok",
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def test_scenario_a_historical_statement_2024(client: TestClient, auth_headers: dict[str, str], monkeypatch) -> None:
    # Set history cutoff to 2026-09-07
    with client.app.state.session_factory() as session:
        set_system_setting(session, "history_cutoff_date", "2026-09-07")

    card = _create_card(client, auth_headers)
    stmt = _create_stmt(client, auth_headers, card["id"], "2024-08-25", "2024-09-14", 50000)

    # 1. statement detail is accessible
    detail_res = client.get(f"/api/v1/statements/{stmt['id']}", headers=auth_headers)
    assert detail_res.status_code == 200
    assert detail_res.json()["remaining_minor"] == 50000

    # 2. analytics retains data
    analytics_res = client.get("/api/v1/analytics?months=24", headers=auth_headers)
    assert analytics_res.status_code == 200

    # 3. Not in current due API
    due_res = client.get("/api/v1/due", headers=auth_headers)
    assert due_res.status_code == 200
    due_ids = [c["statement_id"] for c in due_res.json()["cards"]]
    assert stmt["id"] not in due_ids
    assert due_res.json()["total_remaining_minor"] == 0

    # 4. Not in dashboard due
    dash_res = client.get("/api/v1/dashboard", headers=auth_headers)
    assert dash_res.status_code == 200
    dash_due_ids = [c["statement_id"] for c in dash_res.json()["due"]["cards"]]
    assert stmt["id"] not in dash_due_ids
    assert dash_res.json()["due"]["total_remaining_minor"] == 0

    # 5. Does not send reminder
    dispatched = []
    monkeypatch.setattr(
        "app.services.notification_service.dispatch_notification",
        lambda *args: (dispatched.append(args) or (True, "sent")),
    )
    with client.app.state.session_factory() as session:
        setting = session.scalar(select(NotificationSetting))
        if not setting:
            setting = NotificationSetting()
            session.add(setting)
        setting.enabled = True
        setting.offset_days_json = "[2, 1, 0, -1, -2, -30]"
        setting.provider = "bark"
        session.commit()
        logs = check_and_send_due_reminders(session, today=date(2026, 9, 10))
        assert len(logs) == 0


def test_scenario_b_and_c_boundary_cutoff(client: TestClient, auth_headers: dict[str, str]) -> None:
    with client.app.state.session_factory() as session:
        set_system_setting(session, "history_cutoff_date", "2026-09-07")

    card = _create_card(client, auth_headers)

    # Scenario B: due_date = 2026-09-06 (< cutoff) -> historical
    stmt_b = _create_stmt(client, auth_headers, card["id"], "2026-08-16", "2026-09-06", 20000)

    # Scenario C: due_date = 2026-09-07 (== cutoff) -> active
    stmt_c = _create_stmt(client, auth_headers, card["id"], "2026-08-17", "2026-09-07", 30000)

    due_res = client.get("/api/v1/due", headers=auth_headers).json()
    due_ids = [c["statement_id"] for c in due_res["cards"]]

    assert stmt_b["id"] not in due_ids, "due_date 2026-09-06 should be historical"
    assert stmt_c["id"] in due_ids, "due_date 2026-09-07 should be active"
    assert due_res["total_remaining_minor"] == 30000


def test_scenario_d_active_and_overdue(client: TestClient, auth_headers: dict[str, str]) -> None:
    with client.app.state.session_factory() as session:
        set_system_setting(session, "history_cutoff_date", "2026-09-07")

    card = _create_card(client, auth_headers)

    # Scenario D: due_date = 2026-09-08 unpaid -> active
    stmt_d = _create_stmt(client, auth_headers, card["id"], "2026-08-18", "2026-09-08", 40000)

    due_res = client.get("/api/v1/due", headers=auth_headers).json()
    due_cards = {c["statement_id"]: c for c in due_res["cards"]}

    assert stmt_d["id"] in due_cards
    item = due_cards[stmt_d["id"]]
    assert item["remaining_minor"] == 40000
    assert item["due_date"] == "2026-09-08"


def test_scenario_e_time_advances_cutoff_remains_fixed(client: TestClient, auth_headers: dict[str, str]) -> None:
    with client.app.state.session_factory() as session:
        # Initialized once as 2026-09-07
        set_system_setting(session, "history_cutoff_date", "2026-09-07")

    card = _create_card(client, auth_headers)
    stmt_d = _create_stmt(client, auth_headers, card["id"], "2026-08-18", "2026-09-08", 40000)

    # Time advances to 2026-10-10: call get_history_cutoff_date
    with client.app.state.session_factory() as session:
        cutoff = get_history_cutoff_date(session)
        # Cutoff MUST NOT change to 2026-10-07; it MUST remain 2026-09-07!
        assert cutoff == date(2026, 9, 7)

    # The 2026-09-08 unpaid statement MUST still be active (not historical!)
    due_res = client.get("/api/v1/due", headers=auth_headers).json()
    due_ids = [c["statement_id"] for c in due_res["cards"]]
    assert stmt_d["id"] in due_ids


def test_scenario_f_resync_old_statement_not_in_current_due(client: TestClient, auth_headers: dict[str, str]) -> None:
    with client.app.state.session_factory() as session:
        set_system_setting(session, "history_cutoff_date", "2026-09-07")

    card = _create_card(client, auth_headers)

    # Later sync imports an old 2024 statement
    res = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json={
            "card_id": card["id"],
            "statement_date": "2024-10-01",
            "due_date": "2024-10-20",
            "total_due_minor": 88888,
            "minimum_due_minor": 8888,
            "currency": "CNY",
            "parse_status": "summary_ok",
        },
    )
    assert res.status_code == 201
    old_stmt = res.json()

    # Statement is saved and readable
    detail = client.get(f"/api/v1/statements/{old_stmt['id']}", headers=auth_headers).json()
    assert detail["total_due_minor"] == 88888

    # But does NOT enter current due
    due_res = client.get("/api/v1/due", headers=auth_headers).json()
    due_ids = [c["statement_id"] for c in due_res["cards"]]
    assert old_stmt["id"] not in due_ids


def test_get_dashboard_pure_read_no_mutation(client: TestClient, auth_headers: dict[str, str]) -> None:
    # Remove cutoff from db to test pure read fallback
    with client.app.state.session_factory() as session:
        session.query(SystemSetting).filter(SystemSetting.key == "history_cutoff_date").delete()
        session.commit()

    # GET /dashboard should NOT create any row in system_settings
    res = client.get("/api/v1/dashboard", headers=auth_headers)
    assert res.status_code == 200

    with client.app.state.session_factory() as session:
        row = session.scalar(select(SystemSetting).where(SystemSetting.key == "history_cutoff_date"))
        assert row is None, "GET endpoint must remain pure read without mutating database"

    # Startup / ensure initialization explicitly persists it
    with client.app.state.session_factory() as session:
        val = ensure_history_cutoff_date(session)
        row = session.scalar(select(SystemSetting).where(SystemSetting.key == "history_cutoff_date"))
        assert row is not None
        assert row.value == val.isoformat()
