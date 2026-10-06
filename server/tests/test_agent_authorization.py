import secrets
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute

from app.api.v1 import imap as imap_api
from app.api.v1 import notifications as notification_api
from app.core.plain_secrets import get_secret, set_secret, set_secret_json
from app.core.security import AGENT_ROUTE_ALLOWLIST
from app.models.email_account import EmailAccount
from app.models.notification import NotificationSetting


EXPECTED_AGENT_ACCESS = {
    ("POST", "/api/v1/auth/login"): "public",
    ("POST", "/api/v1/auth/logout"): "owner",
    ("GET", "/api/v1/auth/session"): "public",
    ("GET", "/api/v1/agent-token"): "owner",
    ("POST", "/api/v1/agent-token/generate"): "owner",
    ("POST", "/api/v1/agent-token/reveal"): "owner",
    ("PUT", "/api/v1/agent-token"): "owner",
    ("POST", "/api/v1/catalog/change-requests/{request_id}/approve"): "owner",
    ("POST", "/api/v1/catalog/change-requests/{request_id}/reject"): "owner",
    ("POST", "/api/v1/catalog/change-requests/batch"): "owner",
    ("POST", "/api/v1/catalog/sync"): "owner",
    ("POST", "/api/v1/imap/password/reveal"): "owner",
    ("POST", "/api/v1/notifications/settings"): "owner",
    ("POST", "/api/v1/notifications/test"): "owner",
    ("PATCH", "/api/v1/transactions/{transaction_id}/semantics"): "owner",
    **{
        route: "policy"
        for route in {
            ("POST", "/api/v1/cards"),
            ("GET", "/api/v1/cards/rules"),
            ("POST", "/api/v1/cards/rules/import"),
            ("GET", "/api/v1/cards"),
            ("GET", "/api/v1/cards/calendar-estimates"),
            ("GET", "/api/v1/cards/{card_id}"),
            ("GET", "/api/v1/cards/{card_id}/interest-free-estimate"),
            ("PATCH", "/api/v1/cards/{card_id}"),
            ("GET", "/api/v1/cards/{card_id}/deletion-preview"),
            ("DELETE", "/api/v1/cards/{card_id}"),
            ("POST", "/api/v1/cards/{card_id}/close"),
            ("POST", "/api/v1/cards/{card_id}/reopen"),
            ("POST", "/api/v1/cards/{card_id}/restore"),
            ("GET", "/api/v1/cards/ignored"),
            ("DELETE", "/api/v1/cards/ignored/{bank_code}/{last4}"),
            ("GET", "/api/v1/catalog/stats"),
            ("GET", "/api/v1/catalog/banks"),
            ("GET", "/api/v1/catalog/products"),
            ("GET", "/api/v1/catalog/change-requests"),
            ("POST", "/api/v1/catalog/scan-runs"),
            ("GET", "/api/v1/catalog/scan-runs"),
            ("POST", "/api/v1/catalog/ingest"),
            ("POST", "/api/v1/manual-bills"),
            ("PUT", "/api/v1/manual-bills/{bill_id}"),
            ("DELETE", "/api/v1/manual-bills/{bill_id}"),
            ("GET", "/api/v1/manual-bills"),
            ("GET", "/api/v1/manual-bills/current"),
            ("GET", "/api/v1/manual-bills/marked-paid"),
            ("POST", "/api/v1/manual-bills/generate-current-payments"),
            ("POST", "/api/v1/manual-bills/{bill_id}/mark-paid"),
            ("POST", "/api/v1/manual-bills/{bill_id}/unmark-paid"),
            ("POST", "/api/v1/mortgage/save-bill"),
            ("GET", "/api/v1/mortgage/plans"),
            ("POST", "/api/v1/statements"),
            ("GET", "/api/v1/statements"),
            ("GET", "/api/v1/statements/marked-paid"),
            ("GET", "/api/v1/statements/{statement_id}"),
            ("GET", "/api/v1/statements/{statement_id}/analysis"),
            ("POST", "/api/v1/statements/{statement_id}/repayments"),
            ("POST", "/api/v1/statements/{statement_id}/mark-paid"),
            ("DELETE", "/api/v1/statements/{statement_id}/repayments/{repayment_id}"),
            ("GET", "/api/v1/due"),
            ("GET", "/api/v1/dashboard"),
            ("GET", "/api/v1/imports/providers"),
            ("GET", "/api/v1/imports/batches"),
            ("GET", "/api/v1/imports/batches/{batch_id}"),
            ("POST", "/api/v1/imports/inspect"),
            ("POST", "/api/v1/imports/eml"),
            ("POST", "/api/v1/imports/file"),
            ("GET", "/api/v1/imap/status"),
            ("POST", "/api/v1/imap/sync"),
            ("GET", "/api/v1/imap/runs"),
            ("POST", "/api/v1/imap/test"),
            ("POST", "/api/v1/imap/config"),
            ("GET", "/api/v1/transactions"),
            ("GET", "/api/v1/transactions/{transaction_id}/interest-free"),
            ("GET", "/api/v1/notifications/settings"),
            ("POST", "/api/v1/notifications/trigger"),
            ("GET", "/api/v1/notifications/logs"),
            ("GET", "/api/v1/analytics"),
            ("GET", "/api/v1/backups/settings"),
            ("PUT", "/api/v1/backups/settings"),
            ("POST", "/api/v1/backups/test"),
            ("POST", "/api/v1/backups/run"),
            ("GET", "/api/v1/backups"),
            ("POST", "/api/v1/backups/cleanup"),
            ("DELETE", "/api/v1/backups/{backup_name}"),
            ("POST", "/api/v1/backups/{backup_name}/restore"),
        }
    },
}

AGENT_POLICY_ROUTES = {
    route for route, decision in EXPECTED_AGENT_ACCESS.items() if decision == "policy"
}
AGENT_DENIED_ROUTES = AGENT_POLICY_ROUTES - AGENT_ROUTE_ALLOWLIST


def _agent_headers(client: TestClient | None = None, scope: str = "agent") -> dict[str, str]:
    cleartext = secrets.token_urlsafe(32)
    set_secret("agent", "token", cleartext)
    return {"Authorization": f"Bearer {cleartext}"}


def _is_api_v1_path(path: str) -> bool:
    return path == "/api/v1" or path.startswith("/api/v1/")


def _api_routes(app: FastAPI) -> list[BaseRoute]:
    return [
        route
        for route in app.routes
        if _is_api_v1_path(getattr(route, "path", ""))
    ]


def _declared_agent_access(route: APIRoute) -> list[str]:
    return [
        marker
        for dependency in route.dependant.dependencies
        if (marker := getattr(dependency.call, "agent_access", None)) is not None
    ]


def _assert_route_inventory(
    app: FastAPI, expected: dict[tuple[str, str], str]
) -> None:
    routes = _api_routes(app)
    unsupported = [
        f"{type(route).__name__} {getattr(route, 'path', '<unknown>')}"
        for route in routes
        if not isinstance(route, APIRoute)
    ]
    assert not unsupported, (
        "Unsupported route type under /api/v1: " + ", ".join(unsupported)
    )
    api_routes = [route for route in routes if isinstance(route, APIRoute)]
    actual_keys = {
        (method, route.path) for route in api_routes for method in route.methods
    }
    assert actual_keys == set(expected)
    for route in api_routes:
        for method in route.methods:
            access = expected[(method, route.path)]
            markers = _declared_agent_access(route)
            if access == "public":
                assert markers == []
            else:
                assert markers == [access], (
                    f"{method} {route.path} must declare exactly one Agent access class"
                )


def test_all_api_routes_have_explicit_agent_access(client: TestClient) -> None:
    _assert_route_inventory(client.app, EXPECTED_AGENT_ACCESS)
    assert AGENT_ROUTE_ALLOWLIST <= AGENT_POLICY_ROUTES


def test_inventory_rejects_unclassified_get_and_post() -> None:
    app = FastAPI()

    @app.get("/api/v1/unclassified-get")
    def unclassified_get() -> None:
        return None

    @app.post("/api/v1/unclassified-post")
    def unclassified_post() -> None:
        return None

    expected = {
        ("GET", "/api/v1/unclassified-get"): "policy",
        ("POST", "/api/v1/unclassified-post"): "policy",
    }
    with pytest.raises(AssertionError, match="must declare"):
        _assert_route_inventory(app, expected)


def test_inventory_rejects_mount_under_api_v1() -> None:
    app = FastAPI()
    sub_app = FastAPI()
    sub_app.add_api_route("/secret", lambda: {"secret": True}, methods=["GET"])
    app.mount("/api/v1/internal", sub_app)

    assert TestClient(app).get("/api/v1/internal/secret").status_code == 200
    with pytest.raises(
        AssertionError,
        match=r"Unsupported route type under /api/v1: Mount /api/v1/internal",
    ):
        _assert_route_inventory(app, {})


def test_inventory_rejects_websocket_under_api_v1() -> None:
    app = FastAPI()

    @app.websocket("/api/v1/events")
    async def events() -> None:
        return None

    with pytest.raises(
        AssertionError,
        match=r"Unsupported route type under /api/v1: APIWebSocketRoute /api/v1/events",
    ):
        _assert_route_inventory(app, {})


def test_inventory_does_not_match_similar_api_prefixes() -> None:
    app = FastAPI()
    app.add_api_route("/api/v1/example", lambda: None, methods=["GET"])
    app.add_api_route("/api/v10/example", lambda: None, methods=["GET"])
    app.add_api_route("/api/v100/example", lambda: None, methods=["GET"])

    _assert_route_inventory(app, {("GET", "/api/v1/example"): "public"})


def test_agent_fixed_permissions(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    headers = _agent_headers(client)
    card_payload = {
        "bank_code": "TST",
        "bank_name": "Test Bank",
        "display_name": "Owner Card",
        "last4": "9876",
        "currency": "CNY",
    }

    assert client.get("/api/v1/cards", headers=headers).status_code == 200
    assert client.post(
        "/api/v1/cards", headers=headers, json=card_payload
    ).status_code == 403
    card_res = client.post("/api/v1/cards", headers=auth_headers, json=card_payload)
    assert card_res.status_code == 201
    card = card_res.json()
    assert client.patch(
        f"/api/v1/cards/{card['id']}",
        headers=headers,
        json={"display_name": "Agent Updated"},
    ).status_code == 200
    assert client.get(
        "/api/v1/notifications/settings", headers=headers
    ).status_code == 200


def test_agent_can_run_only_reviewed_bill_and_import_actions(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    headers = _agent_headers(client)
    manual_bill = client.post(
        "/api/v1/manual-bills",
        headers=headers,
        json={
            "bill_type": "rent",
            "name": "Agent-created bill",
            "statement_day": 1,
            "due_day": 28,
            "monthly_amount_minor": 10000,
        },
    )
    assert manual_bill.status_code == 201
    assert client.post(
        "/api/v1/manual-bills/generate-current-payments", headers=headers
    ).status_code == 403
    assert client.post(
        f"/api/v1/manual-bills/{manual_bill.json()['id']}/mark-paid", headers=headers
    ).status_code == 204
    assert client.post(
        f"/api/v1/manual-bills/{manual_bill.json()['id']}/unmark-paid", headers=headers
    ).status_code == 204

    card = client.post(
        "/api/v1/cards",
        headers=auth_headers,
        json={
            "bank_code": "ACT",
            "bank_name": "Action Bank",
            "display_name": "Action Card",
            "last4": "1234",
            "currency": "CNY",
        },
    )
    assert card.status_code == 201
    statement = client.post(
        "/api/v1/statements",
        headers=auth_headers,
        json={
            "card_id": card.json()["id"],
            "statement_date": "2026-09-01",
            "due_date": "2026-09-28",
            "total_due_minor": 10000,
            "currency": "CNY",
        },
    )
    assert statement.status_code == 201
    repayment = client.post(
        f"/api/v1/statements/{statement.json()['id']}/repayments",
        headers=headers,
        json={"amount_minor": 2000, "idempotency_key": "agent-repayment-1"},
    )
    assert repayment.status_code == 403
    marked = client.post(
        f"/api/v1/statements/{statement.json()['id']}/mark-paid",
        headers={**headers, "Idempotency-Key": "agent-mark-paid-1"},
    )
    assert marked.status_code == 200
    assert client.delete(
        f"/api/v1/statements/{statement.json()['id']}/repayments/{marked.json()['id']}",
        headers=headers,
    ).status_code == 204

    invalid_file = {"file": ("invalid.eml", b"not an email", "message/rfc822")}
    assert client.post(
        "/api/v1/imports/inspect", headers=headers, files=invalid_file
    ).status_code != 403
    assert client.post(
        "/api/v1/imports/eml", headers=headers, files=invalid_file
    ).status_code != 403


def test_agent_is_denied_on_every_non_allowlisted_private_route(
    client: TestClient,
) -> None:
    headers = _agent_headers(client)
    route_values = {
        "card_id": "missing",
        "statement_id": "missing",
        "repayment_id": "missing",
        "bill_id": "missing",
        "backup_name": "missing",
        "bank_code": "missing",
        "last4": "missing",
    }
    for method, template in sorted(AGENT_DENIED_ROUTES):
        response = client.request(
            method,
            template.format(**route_values),
            headers=headers,
            json={},
        )
        assert response.status_code == 403, (method, template, response.text)


def test_agents_cannot_call_owner_logout(client: TestClient) -> None:
    response = client.post("/api/v1/auth/logout", headers=_agent_headers(client))
    assert response.status_code == 403


def test_agents_cannot_correct_transaction_semantics(client: TestClient) -> None:
    response = client.patch(
        "/api/v1/transactions/missing/semantics",
        headers=_agent_headers(client),
        json={"transaction_type": "refund"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/api/v1/agent-token", None),
        ("POST", "/api/v1/agent-token/generate", None),
        ("POST", "/api/v1/agent-token/reveal", None),
        ("PUT", "/api/v1/agent-token", {}),
    ],
)
def test_agents_cannot_manage_singleton_token(
    client: TestClient, method: str, path: str, payload: dict | None
) -> None:
    response = client.request(
        method, path, headers=_agent_headers(client), json=payload
    )
    assert response.status_code == 403


def test_agent_is_rejected_before_imap_secret_or_io(
    client: TestClient, monkeypatch
) -> None:
    with client.app.state.session_factory() as session:
        account = EmailAccount(
            name="default",
            host="imap.original.test",
            port=993,
            username="owner@example.test",
            enabled=True,
        )
        session.add(account)
        session.commit()
        set_secret("imap", account.id, "stored-password")

    calls = {"connect": 0}

    class FakeImap:
        def __init__(self, *_args, **_kwargs):
            calls["connect"] += 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def login(self, *_args):
            return None

        def capabilities(self):
            return set()

        def list_folders(self):
            return []

    monkeypatch.setattr("imapclient.IMAPClient", FakeImap)
    headers = _agent_headers(client)

    test_response = client.post(
        "/api/v1/imap/test",
        headers=headers,
        json={"host": "attacker.example.test", "port": 993, "username": ""},
    )
    assert test_response.status_code == 403
    assert calls == {"connect": 0}

    config_response = client.post(
        "/api/v1/imap/config",
        headers=headers,
        json={
            "enabled": True,
            "host": "attacker.example.test",
            "port": 993,
            "username": "owner@example.test",
            "mailbox": "INBOX",
            "all_folders": True,
            "schedule_type": "interval",
            "poll_seconds": 900,
            "daily_time": "08:00",
            "cron_expr": "0 8 * * *",
            "initial_sync_months": 3,
        },
    )
    assert config_response.status_code == 403
    with client.app.state.session_factory() as session:
        assert session.query(EmailAccount).one().host == "imap.original.test"

    schedule_response = client.post(
        "/api/v1/imap/config",
        headers=headers,
        json={
            "enabled": True,
            "schedule_type": "daily",
            "poll_seconds": 1800,
            "daily_time": "09:30",
            "cron_expr": "30 9 * * *",
        },
    )
    assert schedule_response.status_code == 200
    assert schedule_response.json()["status"]["daily_time"] == "09:30"

    monkeypatch.setattr(
        imap_api, "build_imap_runtime_config", lambda *_args: SimpleNamespace()
    )
    monkeypatch.setattr(
        imap_api,
        "sync_mailbox",
        lambda *_args, **_kwargs: SimpleNamespace(
            to_dict=lambda: {"status": "success", "mode": "incremental"}
        ),
    )
    assert client.post("/api/v1/imap/sync", headers=headers).status_code == 200
    assert client.post(
        "/api/v1/imap/sync?full_scan=true", headers=headers
    ).status_code == 403
    assert client.post(
        "/api/v1/imap/sync?months=6", headers=headers
    ).status_code == 403


def test_agent_can_use_reviewed_notification_operations(
    client: TestClient, monkeypatch, auth_headers: dict[str, str]
) -> None:
    with client.app.state.session_factory() as session:
        setting = NotificationSetting(
            enabled=True,
            provider="webhook",
            public_config_json='{"url":"https://original.example.test"}',
        )
        session.add(setting)
        session.commit()
        set_secret_json("notification", setting.id, {"headers": {"X-Key": "secret"}})

    calls = {"assemble": 0, "dispatch": 0, "trigger": 0}

    def fake_assemble(*_args):
        calls["assemble"] += 1
        return {"url": "https://original.example.test", "headers": {"X-Key": "secret"}}

    def fake_test(*_args):
        calls["dispatch"] += 1
        return True, "ok"

    def fake_trigger(*_args, **_kwargs):
        calls["trigger"] += 1
        return []

    monkeypatch.setattr(notification_api, "assemble_provider_config", fake_assemble)
    monkeypatch.setattr(notification_api, "test_notification", fake_test)
    monkeypatch.setattr(
        notification_api, "check_and_send_due_reminders", fake_trigger
    )
    headers = _agent_headers(client)

    # Agent is denied from modifying settings and running tests (Owner-only)
    test_denied = client.post(
        "/api/v1/notifications/test",
        headers=headers,
        json={
            "provider": "webhook",
            "provider_config": {"url": "https://attacker.example.test"},
        },
    )
    assert test_denied.status_code == 403

    settings_denied = client.post(
        "/api/v1/notifications/settings",
        headers=headers,
        json={
            "enabled": False,
            "provider": "disabled",
            "provider_config": {},
        },
    )
    assert settings_denied.status_code == 403

    # Outbound client must not have been invoked
    assert calls == {"assemble": 0, "dispatch": 0, "trigger": 0}

    # Agent CAN trigger reminder check using database-persisted owner configuration
    trigger_res = client.post("/api/v1/notifications/trigger", headers=headers)
    assert trigger_res.status_code == 200
    assert calls["trigger"] == 1

    # Owner can update settings and test provider successfully
    owner_test = client.post(
        "/api/v1/notifications/test",
        headers=auth_headers,
        json={
            "provider": "webhook",
            "provider_config": {"url": "https://valid.example.test"},
        },
    )
    assert owner_test.status_code == 200
    assert calls["dispatch"] == 1

    owner_settings = client.post(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={
            "enabled": True,
            "provider": "webhook",
            "provider_config": {"url": "https://valid.example.test"},
        },
    )
    assert owner_settings.status_code == 200
    with client.app.state.session_factory() as session:
        setting = session.query(NotificationSetting).one()
        assert setting.enabled is True
        assert setting.provider == "webhook"


def test_revoked_agent_token_is_rejected(client: TestClient) -> None:
    headers = _agent_headers(client)
    assert client.get("/api/v1/cards", headers=headers).status_code == 200
    set_secret("agent", "token", None)
    assert client.get("/api/v1/cards", headers=headers).status_code == 401


def test_global_admin_bearer_still_has_full_access(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert client.get("/api/v1/cards", headers=auth_headers).status_code == 200
    assert client.get(
        "/api/v1/notifications/settings", headers=auth_headers
    ).status_code == 200
