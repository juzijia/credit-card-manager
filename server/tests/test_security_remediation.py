import secrets
import pytest
from fastapi.testclient import TestClient

from app.core.plain_secrets import set_secret
from app.services.notification_service import (
    validate_http_endpoint,
    validate_provider_config_endpoints,
)


def _agent_headers(_client: TestClient | None = None) -> dict[str, str]:
    cleartext = secrets.token_urlsafe(32)
    set_secret("agent", "token", cleartext)
    return {"Authorization": f"Bearer {cleartext}"}


def test_notification_http_endpoint_validator() -> None:
    # Accepted HTTP/HTTPS URLs (including LAN and localhost)
    assert validate_http_endpoint("http://192.168.1.20:8080/hook") == "http://192.168.1.20:8080/hook"
    assert validate_http_endpoint("http://localhost:8080/hook") == "http://localhost:8080/hook"
    assert validate_http_endpoint("http://127.0.0.1:9000/webhook") == "http://127.0.0.1:9000/webhook"
    assert validate_http_endpoint("http://nas.local:8080") == "http://nas.local:8080"
    assert validate_http_endpoint("https://api.day.app") == "https://api.day.app"
    assert validate_http_endpoint("https://open.feishu.cn/open-apis/bot/v2/hook/xxx") == "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    assert validate_http_endpoint("") == ""

    # Rejected non-HTTP(S) schemes
    for bad in [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "ftp://example.com/files",
        "gopher://example.com",
        "192.168.1.20:8080",
        "http://",
        "https://",
    ]:
        with pytest.raises(ValueError, match="must use http or https scheme"):
            validate_http_endpoint(bad)


def test_provider_config_endpoints_validation() -> None:
    # Valid webhook config
    validate_provider_config_endpoints("webhook", {"url": "http://192.168.1.20:8080/hook"})
    validate_provider_config_endpoints("gotify", {"url": "https://gotify.example.test"})
    validate_provider_config_endpoints("bark", {"server": "http://localhost:8080"})
    validate_provider_config_endpoints("telegram", {"api_url": "https://api.telegram.org", "proxy_url": "http://127.0.0.1:7890"})
    validate_provider_config_endpoints("telegram", {"api_url": "https://api.telegram.org", "proxy_url": "socks5://127.0.0.1:1080"})
    validate_provider_config_endpoints("telegram", {"api_url": "https://api.telegram.org", "proxy_url": "socks5h://127.0.0.1:1080"})

    # Invalid webhook URL
    with pytest.raises(ValueError, match="must use http or https scheme"):
        validate_provider_config_endpoints("webhook", {"url": "javascript:alert(1)"})

    # Invalid gotify URL
    with pytest.raises(ValueError, match="must use http or https scheme"):
        validate_provider_config_endpoints("gotify", {"url": "file:///etc/passwd"})

    # Invalid bark server
    with pytest.raises(ValueError, match="must use http or https scheme"):
        validate_provider_config_endpoints("bark", {"server": "data:text/html,1"})


def test_notification_settings_owner_only_and_scheme_validation(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    agent_h = _agent_headers(client)

    # 1. Agent attempting to POST settings must be rejected with 403
    agent_res = client.post(
        "/api/v1/notifications/settings",
        headers=agent_h,
        json={
            "enabled": True,
            "provider": "webhook",
            "provider_config": {"url": "http://192.168.1.100:8080/hook"},
        },
    )
    assert agent_res.status_code == 403

    # 2. Owner attempting to set invalid scheme must be rejected with 422
    for bad_url in [
        "javascript:alert(1)",
        "data:text/html,<script>1</script>",
        "file:///etc/passwd",
        "ftp://example.com",
    ]:
        owner_bad = client.post(
            "/api/v1/notifications/settings",
            headers=auth_headers,
            json={
                "enabled": True,
                "provider": "webhook",
                "provider_config": {"url": bad_url},
            },
        )
        assert owner_bad.status_code == 422, f"Failed for {bad_url}: {owner_bad.text}"

    # 3. Owner setting LAN HTTP URL must succeed (HTTP 200)
    owner_lan = client.post(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={
            "enabled": True,
            "provider": "webhook",
            "provider_config": {"url": "http://192.168.1.100:8080/hook"},
        },
    )
    assert owner_lan.status_code == 200


def test_notification_test_owner_only_and_scheme_validation(
    client: TestClient, monkeypatch, auth_headers: dict[str, str]
) -> None:
    agent_h = _agent_headers(client)

    # 1. Agent attempting to call test must be rejected with 403
    agent_res = client.post(
        "/api/v1/notifications/test",
        headers=agent_h,
        json={
            "provider": "webhook",
            "provider_config": {"url": "http://192.168.1.50:8080/test"},
        },
    )
    assert agent_res.status_code == 403

    # 2. Owner attempting invalid scheme must be rejected with 422
    owner_bad = client.post(
        "/api/v1/notifications/test",
        headers=auth_headers,
        json={
            "provider": "webhook",
            "provider_config": {"url": "javascript:void(0)"},
        },
    )
    assert owner_bad.status_code == 422

    # 3. Owner with valid LAN URL passes auth and invokes outbound test
    from app.api.v1 import notifications as notif_api
    monkeypatch.setattr(notif_api, "test_notification", lambda p, c: (True, "mock-sent"))

    owner_ok = client.post(
        "/api/v1/notifications/test",
        headers=auth_headers,
        json={
            "provider": "webhook",
            "provider_config": {"url": "http://192.168.1.50:8080/test"},
        },
    )
    assert owner_ok.status_code == 200
    assert owner_ok.json()["message"] == "mock-sent"


def test_notification_trigger_agent_access_without_destination_override(
    client: TestClient, monkeypatch
) -> None:
    from app.api.v1 import notifications as notif_api
    trigger_called = []
    monkeypatch.setattr(
        notif_api, "check_and_send_due_reminders", lambda s, **kwargs: trigger_called.append(True) or []
    )

    agent_h = _agent_headers(client)

    # Agent CAN trigger check (uses DB-persisted configuration, cannot modify destination via body)
    res = client.post("/api/v1/notifications/trigger", headers=agent_h)
    assert res.status_code == 200
    assert len(trigger_called) == 1


def test_catalog_ingest_source_url_validation(client: TestClient) -> None:
    agent_h = _agent_headers(client)

    # Valid HTTPS URL accepted
    res_valid = client.post(
        "/api/v1/catalog/ingest",
        headers=agent_h,
        json={
            "bank_id": "CMB",
            "candidates": [
                {
                    "bank_id": "CMB",
                    "canonical_name": "Test Safe Card",
                    "sources": [{"title": "Official", "url": "https://example.com/card"}],
                }
            ],
        },
    )
    assert res_valid.status_code == 200
    assert res_valid.json()["success"] is True

    # Valid HTTP URL accepted
    res_http = client.post(
        "/api/v1/catalog/ingest",
        headers=agent_h,
        json={
            "bank_id": "CMB",
            "candidates": [
                {
                    "bank_id": "CMB",
                    "canonical_name": "Test HTTP Card",
                    "sources": [{"title": "Official", "url": "http://legacy-bank.example/card"}],
                }
            ],
        },
    )
    assert res_http.status_code == 200

    # Rejected non-HTTP schemes return 422
    for bad_scheme_url in [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "ftp://example.com/card",
    ]:
        res_bad = client.post(
            "/api/v1/catalog/ingest",
            headers=agent_h,
            json={
                "bank_id": "CMB",
                "candidates": [
                    {
                        "bank_id": "CMB",
                        "canonical_name": "Test Bad Card",
                        "sources": [{"title": "Bad", "url": bad_scheme_url}],
                    }
                ],
            },
        )
        assert res_bad.status_code == 422, f"Expected 422 for {bad_scheme_url}, got {res_bad.status_code}"
