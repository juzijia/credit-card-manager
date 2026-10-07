from datetime import date, datetime
from zoneinfo import ZoneInfo
from fastapi.testclient import TestClient

from app.models.card import Card
from app.models.manual_bill import ManualBill
from app.models.statement import ParseStatus, Statement
from app.main import notification_run_due
from app.notifications.channels import PROVIDERS, dispatch_notification
from app.services.notification_service import (
    check_and_send_due_reminders,
    get_or_create_default_setting,
)


def test_provider_registry_contains_supported_channels() -> None:
    expected_channels = {
        "bark",
        "ntfy",
        "feishu",
        "dingtalk",
        "qywx",
        "serverchan",
        "pushplus",
        "telegram",
        "webhook",
    }
    assert expected_channels <= set(PROVIDERS.keys())
    assert "qinglong" not in PROVIDERS


def test_notification_flow_idempotency_and_unpaid_filter(
    client: TestClient, monkeypatch
) -> None:
    dispatched = []

    def fake_dispatch(provider, config, title, content):
        dispatched.append((provider, title, content))
        return True, "mock sent"

    monkeypatch.setattr(
        "app.services.notification_service.dispatch_notification", fake_dispatch
    )

    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="招商银行 ****9492",
            last4="9492",
            currency="CNY",
        )
        session.add(card)
        session.flush()

        stmt = Statement(
            bank_code="CMB",
            card_id=card.id,
            statement_date=date(2026, 8, 9),
            due_date=date(2026, 8, 27),
            total_due_minor=1680,
            minimum_due_minor=84,
            currency="CNY",
            parse_status=ParseStatus.COMPLETE,
        )
        session.add(stmt)
        session.flush()

        setting = get_or_create_default_setting(session)
        setting.enabled = True
        setting.offset_days_json = "[2, 1, 0]"
        setting.provider = "bark"
        session.commit()

        logs = check_and_send_due_reminders(session, today=date(2026, 8, 27))
        assert len(logs) == 1
        assert logs[0].offset_days == 0
        assert logs[0].status == "success"
        assert len(dispatched) == 1
        assert "招商银行" in dispatched[0][1]

        repeated = check_and_send_due_reminders(session, today=date(2026, 8, 27))
        assert len(repeated) == 0
        assert len(dispatched) == 1

        logs_far = check_and_send_due_reminders(session, today=date(2026, 8, 22))
        assert len(logs_far) == 0


def test_notification_api_endpoints(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    res = client.get("/api/v1/notifications/settings", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert "setting" in data
    assert "providers" in data
    assert any(p["id"] == "bark" for p in data["providers"])

    update_res = client.post(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={
            "enabled": True,
            "offset_days": [3, 1, 0],
            "push_time": "09:30",
            "provider": "webhook",
            "provider_config": {"url": "https://example.test/webhook"},
            "only_if_unpaid": True,
        },
    )
    assert update_res.status_code == 200

    trigger_res = client.post("/api/v1/notifications/trigger", headers=auth_headers)
    assert trigger_res.status_code == 200
    assert "sent_count" in trigger_res.json()


def test_scheduled_notification_respects_push_time() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    assert notification_run_due(
        datetime(2026, 9, 7, 8, 59, tzinfo=timezone), "09:00", None
    ) is False
    assert notification_run_due(
        datetime(2026, 9, 7, 9, 0, tzinfo=timezone), "09:00", None
    ) is True
    assert notification_run_due(
        datetime(2026, 9, 7, 12, 0, tzinfo=timezone),
        "09:00",
        date(2026, 9, 7),
    ) is False


def test_credit_and_manual_bills_are_merged_and_both_logs_are_exposed(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    dispatched = []

    def fake_dispatch(provider, config, title, content):
        dispatched.append((provider, title, content))
        return True, "mock merged sent"

    monkeypatch.setattr(
        "app.services.notification_service.dispatch_notification", fake_dispatch
    )

    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="招商测试卡",
            last4="1234",
            currency="CNY",
        )
        session.add(card)
        session.flush()
        session.add(
            Statement(
                bank_code="CMB",
                card_id=card.id,
                statement_date=date(2026, 8, 9),
                due_date=date(2026, 9, 27),
                total_due_minor=10_000,
                minimum_due_minor=500,
                currency="CNY",
                parse_status=ParseStatus.COMPLETE,
            )
        )
        session.add(
            ManualBill(
                bill_type="房租",
                name="测试房租",
                statement_day=1,
                due_day=27,
                monthly_amount_minor=20_000,
                reminder_enabled=True,
                enabled=True,
            )
        )
        setting = get_or_create_default_setting(session)
        setting.enabled = True
        setting.offset_days_json = "[0]"
        setting.provider = "bark"
        session.commit()

        logs = check_and_send_due_reminders(session, today=date(2026, 9, 27))
        assert len(logs) == 2
        assert len(dispatched) == 1
        assert "信用卡待还清单" in dispatched[0][2]
        assert "手动常规账单" in dispatched[0][2]

        repeated = check_and_send_due_reminders(session, today=date(2026, 9, 27))
        assert repeated == []
        assert len(dispatched) == 1

    response = client.get("/api/v1/notifications/logs", headers=auth_headers)
    assert response.status_code == 200
    assert {item["kind"] for item in response.json()} == {
        "credit_card",
        "manual_bill",
    }


def test_gotify_and_pushdeer_report_http_failure_status(monkeypatch) -> None:
    class FakeResponse:
        status_code = 500
        def json(self):
            return {"code": 500, "message": "error"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("app.notifications.channels.httpx.Client", FakeClient)
    ok_g, msg_g = dispatch_notification("gotify", {"url": "https://g.invalid", "token": "t"}, "title", "content")
    assert ok_g is False
    assert "500" in msg_g

    ok_p, msg_p = dispatch_notification("pushdeer", {"key": "k"}, "title", "content")
    assert ok_p is False
    assert "500" in msg_p


def test_notification_toggle_disabled_preserves_stored_credentials(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    save_res = client.post(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={
            "enabled": True,
            "offset_days": [2, 1, 0],
            "push_time": "09:00",
            "provider": "bark",
            "provider_config": {"key": "secret-bark-key-preserved"},
            "only_if_unpaid": True,
        },
    )
    assert save_res.status_code == 200

    disable_res = client.post(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={"provider": "disabled", "offset_days": [2, 1, 0], "push_time": "09:00"},
    )
    assert disable_res.status_code == 200

    # Re-enable without resending key (omitting key keeps existing vault secret)
    re_enable_res = client.post(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={
            "enabled": True,
            "offset_days": [2, 1, 0],
            "push_time": "09:00",
            "provider": "bark",
            "provider_config": {},
            "only_if_unpaid": True,
        },
    )
    assert re_enable_res.status_code == 200
    status_res = client.get("/api/v1/notifications/settings", headers=auth_headers)
    assert status_res.json()["setting"]["enabled"] is True
    assert status_res.json()["setting"]["configured"] is True

    from app.models.notification import NotificationSetting
    from app.services.notification_service import assemble_provider_config
    with client.app.state.session_factory() as session:
        setting = session.query(NotificationSetting).first()
        cfg = assemble_provider_config(setting)
        assert cfg.get("key") == "secret-bark-key-preserved"

    # Explicit empty string clears stored secret
    clear_res = client.post(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={
            "enabled": True,
            "offset_days": [2, 1, 0],
            "push_time": "09:00",
            "provider": "bark",
            "provider_config": {"key": ""},
            "only_if_unpaid": True,
        },
    )
    assert clear_res.status_code == 200
    status_cleared = client.get("/api/v1/notifications/settings", headers=auth_headers)
    assert status_cleared.json()["setting"]["configured"] is False


def test_card_reminder_switch_is_honored(client: TestClient, monkeypatch) -> None:
    dispatched = []
    monkeypatch.setattr(
        "app.services.notification_service.dispatch_notification",
        lambda *args: (dispatched.append(args) or (True, "mock sent")),
    )
    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="关闭提醒的卡",
            last4="1234",
            currency="CNY",
            reminder_enabled=False,
        )
        session.add(card)
        session.flush()
        session.add(
            Statement(
                bank_code="CMB",
                card_id=card.id,
                statement_date=date(2026, 8, 9),
                due_date=date(2026, 9, 27),
                total_due_minor=10_000,
                minimum_due_minor=500,
                currency="CNY",
                parse_status=ParseStatus.COMPLETE,
            )
        )
        setting = get_or_create_default_setting(session)
        setting.enabled = True
        setting.offset_days_json = "[0]"
        session.commit()

        assert check_and_send_due_reminders(
            session, today=date(2026, 9, 27)
        ) == []
        assert dispatched == []


def test_notification_disabled_provider_api(client: TestClient, auth_headers: dict[str, str]) -> None:
    res = client.get("/api/v1/notifications/settings", headers=auth_headers)
    assert res.status_code == 200
    providers = res.json()["providers"]
    assert any(p["id"] == "disabled" and p["name"] == "已关闭" for p in providers)

    save_res = client.post(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={"provider": "disabled", "offset_days": [2, 1, 0], "push_time": "09:00"}
    )
    assert save_res.status_code == 200
    assert save_res.json()["success"] is True

    check_res = client.get("/api/v1/notifications/settings", headers=auth_headers)
    assert check_res.json()["setting"]["enabled"] is False
    assert check_res.json()["setting"]["provider"] == "disabled"

    test_res = client.post(
        "/api/v1/notifications/test",
        headers=auth_headers,
        json={"provider": "disabled", "provider_config": {}}
    )
    assert test_res.status_code == 200
    assert test_res.json()["success"] is False


def test_overdue_reminders_and_offset_validation(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    save_res = client.post(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={"provider": "disabled", "offset_days": [2, 1, 0, -1, -2, -3], "push_time": "09:00"}
    )
    assert save_res.status_code == 200
    get_res = client.get("/api/v1/notifications/settings", headers=auth_headers)
    assert get_res.json()["setting"]["offset_days"] == [2, 1, 0, -1, -2, -3]

    bad_res = client.post(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={"provider": "disabled", "offset_days": [-31], "push_time": "09:00"}
    )
    assert bad_res.status_code == 422

    dispatched = []
    monkeypatch.setattr(
        "app.services.notification_service.dispatch_notification",
        lambda provider, config, title, content: (dispatched.append((title, content)) or (True, "sent")),
    )

    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="招行白金卡",
            currency="CNY",
            last4="8888",
            due_day=25,
            reminder_enabled=True,
        )
        session.add(card)
        session.flush()

        statement = Statement(
            bank_code="CMB",
            card_id=card.id,
            statement_date=date(2026, 8, 5),
            due_date=date(2026, 8, 25),
            total_due_minor=500000,
            currency="CNY",
        )
        session.add(statement)

        setting = get_or_create_default_setting(session)
        setting.enabled = True
        setting.offset_days_json = "[0, -1, -2]"
        setting.provider = "bark"
        session.commit()

        logs = check_and_send_due_reminders(session, today=date(2026, 8, 27))
        assert len(logs) == 1
        assert logs[0].offset_days == -2
        assert logs[0].status == "success"

        assert len(dispatched) == 1
        title, content = dispatched[0]
        assert "🚨【逾期警报】已逾期 2 天" in title
        assert "⚠️ 已逾期 2 天" in content
        assert "🚨 该账单已发生逾期！" in content


def test_concurrent_notification_check_sends_no_duplicates(
    client: TestClient, monkeypatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    dispatched = []
    monkeypatch.setattr(
        "app.services.notification_service.dispatch_notification",
        lambda *args: (dispatched.append(args) or (True, "mock sent")),
    )

    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="并发测试卡",
            last4="5555",
            currency="CNY",
            reminder_enabled=True,
        )
        session.add(card)
        session.flush()

        stmt = Statement(
            bank_code="CMB",
            card_id=card.id,
            statement_date=date(2026, 8, 9),
            due_date=date(2026, 8, 27),
            total_due_minor=10000,
            currency="CNY",
        )
        session.add(stmt)

        setting = get_or_create_default_setting(session)
        setting.enabled = True
        setting.offset_days_json = "[0]"
        setting.provider = "bark"
        session.commit()

    def run_check():
        with client.app.state.session_factory() as s:
            return check_and_send_due_reminders(s, today=date(2026, 8, 27))

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_check)
        f2 = executor.submit(run_check)
        r1 = f1.result()
        r2 = f2.result()

    total_logs = len(r1) + len(r2)
    assert total_logs == 1
    assert len(dispatched) == 1
