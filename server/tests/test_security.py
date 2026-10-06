import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.plain_secrets import get_secret, get_secret_json, set_secret, set_secret_json
from app.core.redaction import REDACTED, redact_sensitive
from app.db.base import Base
from app.main import create_app
from app.models.email_account import EmailAccount
from app.models.notification import NotificationSetting


def test_notification_read_dto_structure_and_db_separation(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    secrets = {
        "bot_token": "123456:real-looking-secret",
    }
    public = {
        "chat_id": "private-chat-id",
        "api_url": "https://api.telegram.example",
    }
    with client.app.state.session_factory() as session:
        setting = NotificationSetting(
            enabled=True,
            provider="telegram",
            public_config_json=json.dumps(public),
        )
        session.add(setting)
        session.commit()
        set_secret_json("notification", setting.id, secrets)

    response = client.get("/api/v1/notifications/settings", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()["setting"]
    assert payload["provider"] == "telegram"
    assert payload["configured"] is True
    assert payload["chat_id_configured"] is True

    # Database public config must only hold public fields, not secrets
    with client.app.state.session_factory() as session:
        db_setting = session.query(NotificationSetting).one()
        db_public = json.loads(db_setting.public_config_json)
        assert db_public == public
        assert "bot_token" not in db_public
        assert not hasattr(db_setting, "secret_ciphertext")


def test_omitted_notification_write_preserves_stored_secret(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    stored_token = "keep-this-token-server-side"
    with client.app.state.session_factory() as session:
        setting = NotificationSetting(
            enabled=True,
            provider="telegram",
            public_config_json="{}",
        )
        session.add(setting)
        session.commit()
        setting_id = setting.id
        set_secret_json("notification", setting_id, {"bot_token": stored_token})

    # Preserve-on-omit semantics: omitting bot_token keeps the stored token in vault
    response = client.post(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={
            "enabled": True,
            "offset_days": [2, 1, 0],
            "push_time": "09:00",
            "provider": "telegram",
            "provider_config": {},
            "only_if_unpaid": True,
        },
    )

    assert response.status_code == 200
    vault_secret = get_secret_json("notification", setting_id)
    assert vault_secret is not None
    assert vault_secret.get("bot_token") == stored_token


def test_imap_config_saves_to_email_account_and_not_env(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/imap/config",
        headers=auth_headers,
        json={
            "enabled": True,
            "host": "imap.test.org",
            "port": 993,
            "username": "user@test.org",
            "password": "secret-app-password",
            "mailbox": "INBOX",
            "all_folders": True,
            "schedule_type": "interval",
            "poll_seconds": 900,
            "daily_time": "08:00",
            "cron_expr": "0 8 * * *",
            "initial_sync_months": 3,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"]["configured"] is True
    assert "secret-app-password" not in response.text

    # Verify EmailAccount in DB stores connection details but no password column
    with client.app.state.session_factory() as session:
        account = session.query(EmailAccount).filter_by(name="default").one()
        assert account.host == "imap.test.org"
        assert account.username == "user@test.org"
        assert not hasattr(account, "secret_ciphertext")
        assert get_secret("imap", account.id) == "secret-app-password"

    # Verify .env was not written with plaintext password
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists():
        assert "secret-app-password" not in env_file.read_text(encoding="utf-8")


def test_imap_status_never_returns_password(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    with client.app.state.session_factory() as session:
        account = EmailAccount(
            name="default",
            host="imap.example.test",
            port=993,
            username="private@example.test",
            enabled=True,
        )
        session.add(account)
        session.commit()
        set_secret("imap", account.id, "private-app-password")

    response = client.get("/api/v1/imap/status", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["username_configured"] is True
    assert response.json()["username"] == "private@example.test"
    assert "password" not in response.json()
    assert "private-app-password" not in response.text


def test_token_mode_enforces_admin_boundary(tmp_path: Path) -> None:
    token = "unique-admin-token-that-is-at-least-32-characters"
    settings = Settings(
        auth_mode="token",
        admin_token=token,
        database_url=f"sqlite:///{(tmp_path / 'token-mode.db').as_posix()}",
        background_tasks_enabled=False,
        error_log_path=None,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)

    with TestClient(app) as protected:
        assert protected.get("/health").status_code == 200
        assert protected.get("/api/v1/cards").status_code == 401
        assert protected.get(
            "/api/v1/cards", headers={"Authorization": "Bearer wrong-token"}
        ).status_code == 401
        assert protected.get(
            "/api/v1/cards", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200


def test_token_mode_rejects_missing_or_short_token() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_mode="token", admin_token=None)
    with pytest.raises(ValidationError):
        Settings(auth_mode="token", admin_token="short")


def test_redaction_covers_credentials_urls_and_complete_card_numbers() -> None:
    raw = (
        "Authorization: Bearer bearer-value "
        "https://example.test/send?token=query-value&safe=yes "
        "https://api.telegram.org/bot123456:token-value/sendMessage "
        "card 6222 0202 0123 4567"
    )

    safe = redact_sensitive(raw)

    assert REDACTED in safe
    assert "bearer-value" not in safe
    assert "query-value" not in safe
    assert "123456:token-value" not in safe
    assert "6222 0202 0123 4567" not in safe
    assert "****4567" in safe


def test_legacy_email_account_api_is_removed(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert client.get("/api/v1/email-accounts", headers=auth_headers).status_code == 404
    assert client.post("/api/v1/email-accounts", headers=auth_headers, json={}).status_code == 404
