from datetime import date, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.manual_bill import ManualBillPayment
from app.services.statements import local_today


def create_card(
    client: TestClient, auth_headers: dict[str, str], *, hidden: bool = False
) -> dict:
    response = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "招商经典白",
            "last4": "1234",
            "currency": "CNY",
            "credit_limit_minor": 10000000,
            "hidden": hidden,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_statement(
    client: TestClient,
    auth_headers: dict[str, str],
    card_id: str,
    total_due_minor: int = 123456,
) -> dict:
    tz = client.app.state.settings.timezone
    today = local_today(tz)
    response = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json={
            "card_id": card_id,
            "statement_date": today.isoformat(),
            "due_date": (today + timedelta(days=10)).isoformat(),
            "total_due_minor": total_due_minor,
            "minimum_due_minor": min(total_due_minor, 12345),
            "currency": "CNY",
            "parse_status": "summary_ok",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_health_and_local_api_are_available_without_login(client: TestClient, auth_headers: dict[str, str]) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/v1/cards").status_code == 401
    assert client.get("/api/v1/cards", headers=auth_headers).status_code == 200


def test_fresh_install_defaults(client: TestClient, auth_headers: dict[str, str]) -> None:
    assert client.get("/api/v1/cards", headers=auth_headers).json() == []

    imap = client.get("/api/v1/imap/status", headers=auth_headers).json()
    assert imap["schedule_type"] == "interval"
    assert imap["poll_seconds"] == 1800
    assert imap["initial_sync_months"] == 12

    notifications = client.get("/api/v1/notifications/settings", headers=auth_headers).json()["setting"]
    assert notifications["provider"] == "disabled"
    assert notifications["offset_days"] == [2, 1, 0, -1, -2]


def test_vue_web_assets_are_served_from_repository_web_directory(
    client: TestClient,
) -> None:
    index = client.get("/")
    assert index.status_code == 200
    assert "cdn.jsdelivr.net/npm/vue@3" in index.text
    assert "flutter_bootstrap.js" not in index.text
    assert "SQLite" in index.text
    assert "免密本地预览 (Dev Mode)" not in index.text
    assert "v-if=\"activeTab === 'settings'\" class=\"space-y-6 w-full\"" in index.text
    assert "github.com/juzijia/credit-card-manager" in index.text
    assert "选择还款渠道" in index.text
    assert "不代表系统已核验实际付款" not in index.text
    assert 'data-channel-icon="bank-app"' in index.text
    assert "<use :href=" not in index.text
    assert "{ value: null, label: '未记录' }" not in index.text
    assert "＋ 添加还款记录" in index.text
    assert "还款记录已添加" in index.text

    static_asset = client.get("/static/favicon.svg")
    assert static_asset.status_code == 200
    assert "image/svg+xml" in static_asset.headers["content-type"]

    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200
    assert "image/svg+xml" in favicon.headers["content-type"]


def test_dashboard_get_has_no_database_write_side_effect(client: TestClient, auth_headers: dict[str, str]) -> None:
    with client.app.state.session_factory() as session:
        before = session.scalar(select(func.count()).select_from(ManualBillPayment))
    assert client.get("/api/v1/dashboard", headers=auth_headers).status_code == 200
    with client.app.state.session_factory() as session:
        after = session.scalar(select(func.count()).select_from(ManualBillPayment))
    assert after == before


def test_card_currency_accepts_iso_code_and_rejects_invalid(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # Phase 2 currency safety: a card may carry a non-CNY currency code
    # (Card.currency stays a non-authoritative legacy field), but malformed
    # currency codes must still be rejected.
    valid = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "外币卡",
            "last4": "9999",
            "currency": "USD",
        },
    )
    assert valid.status_code == 201
    assert valid.json()["currency"] == "USD"

    invalid = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "坏币卡",
            "last4": "9998",
            "currency": "RMB1",
        },
    )
    assert invalid.status_code == 422


def test_lowercase_currency_is_normalized(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "小写币卡",
            "last4": "9997",
            "currency": "usd",
        },
    )
    assert response.status_code == 201
    assert response.json()["currency"] == "USD"


def test_money_remains_integer_and_due_is_aggregated(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    card = create_card(client, auth_headers)
    statement = create_statement(client, auth_headers, card["id"], 900719925474099)

    due = client.get("/api/v1/due", headers=auth_headers)
    assert due.status_code == 200
    payload = due.json()
    assert payload["total_remaining_minor"] == 900719925474099
    assert payload["cards"][0]["statement_id"] == statement["id"]


def test_hidden_card_is_excluded_but_reminder_setting_is_independent(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    card = create_card(client, auth_headers, hidden=True)
    assert card["reminder_enabled"] is True
    create_statement(client, auth_headers, card["id"])

    due = client.get("/api/v1/due", headers=auth_headers).json()
    assert due["total_remaining_minor"] == 0
    assert due["cards"] == []


def test_partial_repayment_and_mark_paid_are_idempotent(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    card = create_card(client, auth_headers)
    statement = create_statement(client, auth_headers, card["id"], 10000)

    partial = client.post(
        f"/api/v1/statements/{statement['id']}/repayments",
        headers=auth_headers,
        json={
            "amount_minor": 2500,
            "repayment_channel": "bank_app",
            "idempotency_key": "partial-0001",
        },
    )
    assert partial.status_code == 201
    assert partial.json()["repayment_channel"] == "bank_app"

    mark_headers = {**auth_headers, "Idempotency-Key": "mark-paid-0001"}
    first = client.post(
        f"/api/v1/statements/{statement['id']}/mark-paid",
        headers=mark_headers,
        json={"repayment_channel": "alipay"},
    )
    second = client.post(
        f"/api/v1/statements/{statement['id']}/mark-paid", headers=mark_headers
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["amount_minor"] == 7500
    assert first.json()["repayment_channel"] == "alipay"

    result = client.get(
        f"/api/v1/statements/{statement['id']}", headers=auth_headers
    ).json()
    assert result["paid_minor"] == 10000
    assert result["remaining_minor"] == 0
    assert result["status"] == "paid"

    marked_paid = client.get(
        "/api/v1/statements/marked-paid", headers=auth_headers
    )
    assert marked_paid.status_code == 200
    assert [item["repayment_id"] for item in marked_paid.json()] == [
        first.json()["id"]
    ]
    assert marked_paid.json()[0]["repayment_channel"] == "alipay"

    undone = client.delete(
        f"/api/v1/statements/{statement['id']}/repayments/{first.json()['id']}",
        headers=auth_headers,
    )
    assert undone.status_code == 204
    restored = client.get(
        f"/api/v1/statements/{statement['id']}", headers=auth_headers
    ).json()
    assert restored["paid_minor"] == 2500
    assert restored["remaining_minor"] == 7500


def test_overpayment_is_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    card = create_card(client, auth_headers)
    statement = create_statement(client, auth_headers, card["id"], 10000)
    response = client.post(
        f"/api/v1/statements/{statement['id']}/repayments",
        headers=auth_headers,
        json={"amount_minor": 10001, "idempotency_key": "too-much-0001"},
    )
    assert response.status_code == 422


def test_mark_paid_rejects_unknown_repayment_channel(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    card = create_card(client, auth_headers)
    statement = create_statement(client, auth_headers, card["id"], 10000)
    response = client.post(
        f"/api/v1/statements/{statement['id']}/mark-paid",
        headers={**auth_headers, "Idempotency-Key": "invalid-channel-1"},
        json={"repayment_channel": "cash"},
    )
    assert response.status_code == 422


def test_openapi_schema_can_be_generated(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/api/v1/due" in response.json()["paths"]


def test_card_identity_is_unique_and_shared_limit_uses_bank_maximum(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    first = create_card(client, auth_headers)
    second_response = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "招商第二张卡",
            "last4": "5678",
            "currency": "CNY",
            "credit_limit_minor": 8_000_000,
        },
    )
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()

    duplicate = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "重复身份",
            "last4": "1234",
            "currency": "CNY",
        },
    )
    assert duplicate.status_code == 409

    create_statement(client, auth_headers, first["id"], 10_000)
    create_statement(client, auth_headers, second["id"], 20_000)
    dashboard = client.get("/api/v1/dashboard", headers=auth_headers).json()

    assert dashboard["total_credit_limit_minor"] == 10_000_000
    assert len(dashboard["credit_limit_banks"]) == 1
    assert dashboard["due"]["total_remaining_minor"] == 30_000
    assert {item["card_id"] for item in dashboard["due"]["cards"]} == {
        first["id"],
        second["id"],
    }


def test_card_product_dictionary_matches_alias_and_manual_override_wins(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    created = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CMB",
            "bank_name": "招商银行",
            "display_name": "全币种测试卡",
            "last4": "1234",
            "currency": "CNY",
            "card_product": "全币种国际信用卡",
        },
    )
    assert created.status_code == 201, created.text
    card = created.json()
    assert card["annual_fee_minor"] == 0
    assert card["annual_fee_source"] == "dictionary"

    manual = client.patch(
        f"/api/v1/cards/{card['id']}",
        headers=auth_headers,
        json={
            "annual_fee_minor": 12300,
            "annual_fee_policy": "用户确认的特殊政策",
        },
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["annual_fee_source"] == "manual"

    changed_product = client.patch(
        f"/api/v1/cards/{card['id']}",
        headers=auth_headers,
        json={"card_product": "经典白金卡"},
    )
    assert changed_product.status_code == 200, changed_product.text
    assert changed_product.json()["annual_fee_minor"] == 12300
    assert changed_product.json()["annual_fee_policy"] == "用户确认的特殊政策"
    assert changed_product.json()["annual_fee_source"] == "manual"


def test_card_due_date_rule_requires_a_valid_pair(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    created = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "CCB",
            "bank_name": "建设银行",
            "display_name": "规则测试卡",
            "last4": "1234",
            "due_date_rule_type": "fixed_day",
            "due_date_rule_value": 6,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["due_date_rule_type"] == "fixed_day"
    assert created.json()["due_date_rule_value"] == 6

    incomplete = client.patch(
        f"/api/v1/cards/{created.json()['id']}",
        headers=auth_headers,
        json={"due_date_rule_type": "days_after_statement"},
    )
    assert incomplete.status_code == 422


def test_current_month_marked_paid_timezone_boundary(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    from datetime import datetime, timezone
    from app.models.card import Card
    from app.models.repayment import Repayment
    from app.models.statement import Statement

    tz = client.app.state.settings.timezone
    today = local_today(tz)
    first_day = today.replace(day=1)
    local_tz = ZoneInfo(tz)

    early_this_month_local = datetime(first_day.year, first_day.month, 1, 1, 0, tzinfo=local_tz)
    early_this_month_utc = early_this_month_local.astimezone(timezone.utc).replace(tzinfo=None)

    late_prev_month_local = datetime(first_day.year, first_day.month, 1, 0, 0, tzinfo=local_tz)
    late_prev_month_utc = (late_prev_month_local - timedelta(hours=1)).astimezone(timezone.utc).replace(tzinfo=None)

    with client.app.state.session_factory() as session:
        card = Card(
            bank_code="CMB",
            bank_name="招商银行",
            display_name="时区测试卡",
            last4="7777",
            currency="CNY",
        )
        session.add(card)
        session.flush()

        stmt = Statement(
            bank_code=card.bank_code,
            card_id=card.id,
            statement_date=first_day,
            due_date=first_day,
            total_due_minor=10000,
            currency="CNY",
        )
        session.add(stmt)
        session.flush()

        r1 = Repayment(
            statement_id=stmt.id,
            amount_minor=5000,
            paid_at=early_this_month_utc,
            note="Marked paid in full",
            idempotency_key="test-tz-key-1",
        )
        r2 = Repayment(
            statement_id=stmt.id,
            amount_minor=5000,
            paid_at=late_prev_month_utc,
            note="Marked paid in full",
            idempotency_key="test-tz-key-2",
        )
        session.add_all([r1, r2])
        session.commit()
        r1_id = r1.id
        r2_id = r2.id

    res = client.get("/api/v1/statements/marked-paid", headers=auth_headers)
    assert res.status_code == 200
    returned_ids = [item["repayment_id"] for item in res.json()]
    assert r1_id in returned_ids
    assert r2_id not in returned_ids


def test_historical_statement_marked_paid_excluded_from_current_month(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    card = create_card(client, auth_headers)
    tz = client.app.state.settings.timezone
    today = local_today(tz)
    # Statement from 6 months ago
    hist_date = today - timedelta(days=180)
    res = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json={
            "card_id": card["id"],
            "statement_date": hist_date.isoformat(),
            "due_date": (hist_date + timedelta(days=20)).isoformat(),
            "total_due_minor": 50000,
            "minimum_due_minor": 5000,
            "currency": "CNY",
            "parse_status": "summary_ok",
        },
    )
    assert res.status_code == 201
    hist_stmt = res.json()

    mark_res = client.post(
        f"/api/v1/statements/{hist_stmt['id']}/mark-paid",
        headers={**auth_headers, "Idempotency-Key": "hist-mark-001"},
    )
    assert mark_res.status_code == 200
    repayment_id = mark_res.json()["id"]

    marked_paid = client.get("/api/v1/statements/marked-paid", headers=auth_headers)
    assert marked_paid.status_code == 200
    ids = [item["repayment_id"] for item in marked_paid.json()]
    assert repayment_id not in ids
