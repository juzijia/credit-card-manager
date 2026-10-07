import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.core.notification_secrets import is_credential_bearing_url
from app.core.plain_secrets import get_secret_json
from app.db.base import Base
from app.main import create_app
from app.models.notification import NotificationSetting
from app.services.notification_service import separate_provider_config

OWNER_TOKEN = SecretStr("synthetic-owner-token-" + "x" * 32)
ADMIN_TOKEN = SecretStr("synthetic-admin-token-" + "x" * 32)


@pytest.fixture
def admin_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        env="production",
        auth_mode="session",
        owner_token=OWNER_TOKEN,
        admin_token=ADMIN_TOKEN,
        allowed_host="testserver",
        database_url=f"sqlite:///{(tmp_path / 'sec.db').as_posix()}",
        error_log_path=None,
        background_tasks_enabled=False,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as test_client:
        yield test_client
    app.state.engine.dispose()


# ---------------------------------------------------------------------------
# F02 Tests: Notification secret classification & credential-bearing URLs
# ---------------------------------------------------------------------------


def test_is_credential_bearing_url_detects_credentials() -> None:
    # 1. URL userinfo
    assert is_credential_bearing_url("https://user:password@example.com/webhook")
    assert is_credential_bearing_url("http://admin:secret123@127.0.0.1:8080/push")

    # 2. Query parameters with token / secret / key
    assert is_credential_bearing_url("https://oapi.dingtalk.com/robot/send?access_token=abcd1234")
    assert is_credential_bearing_url("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=mykey")
    assert is_credential_bearing_url("https://example.com/hook?token=secret-token-val")
    assert is_credential_bearing_url("https://example.com/hook?api_key=sk-12345")
    assert is_credential_bearing_url("https://example.com/hook?sign=someSignature123")

    # 3. Path segments indicating tokens or bots
    assert is_credential_bearing_url("https://api.telegram.org/bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11/sendMessage")
    assert is_credential_bearing_url("https://discord.com/api/webhooks/123456789/token_secret_here")
    assert is_credential_bearing_url("https://example.com/%74oken/abc")
    assert is_credential_bearing_url("https://example.com/%61ccess_token/abc")
    assert is_credential_bearing_url("https://example.com/%2FBoT123%3AABC/send")
    assert is_credential_bearing_url("https://example.com/hook#access_token=abc")
    assert is_credential_bearing_url("https://example.com/hook#token=abc")
    assert is_credential_bearing_url("https://example.com/hook#api_key=abc")

    # 4. Normal benign URLs without credentials
    assert not is_credential_bearing_url("https://example.com/api/v1/notify")
    assert not is_credential_bearing_url("https://api.day.app")
    assert not is_credential_bearing_url("http://192.168.1.100:8080/notify?priority=high")
    assert not is_credential_bearing_url("https://ntfy.sh/my_public_topic")
    assert not is_credential_bearing_url("https://example.com/monkey/authentication#settings")


def test_separate_provider_config_moves_credential_url_to_secret() -> None:
    # Webhook with plain benign URL -> remains in public
    plain_cfg = {"url": "https://example.com/hook"}
    pub, sec = separate_provider_config("webhook", plain_cfg)
    assert "url" in pub
    assert pub["url"] == "https://example.com/hook"
    assert "url" not in sec

    # Webhook with credential in query -> downgraded to secret
    cred_cfg = {"url": "https://example.com/hook?token=super-secret-query-token"}
    pub2, sec2 = separate_provider_config("webhook", cred_cfg)
    assert "url" not in pub2
    assert "url" in sec2
    assert sec2["url"] == "https://example.com/hook?token=super-secret-query-token"

    # Webhook with basic auth userinfo -> downgraded to secret
    auth_cfg = {"url": "https://bot_user:pass123@example.com/hook"}
    pub3, sec3 = separate_provider_config("webhook", auth_cfg)
    assert "url" not in pub3
    assert "url" in sec3

    # Telegram with proxy_url bearing password -> proxy_url downgraded to secret
    tg_cfg = {
        "chat_id": "123456",
        "proxy_url": "http://user:secretpass@127.0.0.1:1080",
    }
    pub4, sec4 = separate_provider_config("telegram", tg_cfg)
    assert "proxy_url" not in pub4
    assert "proxy_url" in sec4
    assert pub4["chat_id"] == "123456"

    # Telegram with clean proxy_url -> remains in public
    tg_clean = {
        "chat_id": "123456",
        "proxy_url": "socks5://127.0.0.1:1080",
    }
    pub5, sec5 = separate_provider_config("telegram", tg_clean)
    assert pub5["proxy_url"] == "socks5://127.0.0.1:1080"
    assert "proxy_url" not in sec5


def test_separate_provider_config_treats_unknown_fields_and_headers_as_secret() -> None:
    cfg = {
        "url": "https://example.com/hook",
        "headers": {"Authorization": "Bearer sensitive-token"},
        "custom_secret_key": "my-secret-value",
    }
    pub, sec = separate_provider_config("webhook", cfg)
    assert pub == {"url": "https://example.com/hook"}
    assert sec["headers"] == {"Authorization": "Bearer sensitive-token"}
    assert sec["custom_secret_key"] == "my-secret-value"

    # Unknown provider defaults completely to secret
    unknown_cfg = {"foo": "bar", "url": "https://example.com"}
    pub_u, sec_u = separate_provider_config("nonexistent_provider", unknown_cfg)
    assert pub_u == {}
    assert sec_u == unknown_cfg


def test_notification_settings_storage_separates_secrets(admin_client: TestClient) -> None:
    # Save a webhook with credential query and custom headers
    resp = admin_client.post(
        "/api/v1/notifications/settings",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN.get_secret_value()}"},
        json={
            "provider": "webhook",
            "enabled": True,
            "offset_days": [0, 1],
            "push_time": "09:00",
            "only_if_unpaid": True,
            "provider_config": {
                "url": "https://example.com/hook?token=leaked_secret_token",
                "headers": {"X-Secret": "sensitive_header"},
                "normal_field": "clean_value",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True

    # Reading settings via GET returns configured status adhering to schema
    get_resp = admin_client.get(
        "/api/v1/notifications/settings",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN.get_secret_value()}"},
    )
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["setting"]["configured"] is True

    # Updating the same provider must keep the existing credential URL secret.
    update_resp = admin_client.post(
        "/api/v1/notifications/settings",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN.get_secret_value()}"},
        json={
            "provider": "webhook",
            "enabled": True,
            "offset_days": [0],
            "push_time": "10:00",
            "only_if_unpaid": True,
            "provider_config": {},
        },
    )
    assert update_resp.status_code == 200

    # Verify directly in database that public_config_json does not hold the credential URL or headers
    with admin_client.app.state.session_factory() as session:
        setting = session.query(NotificationSetting).first()
        assert setting is not None
        pub_db = json.loads(setting.public_config_json or "{}")
        assert "url" not in pub_db
        assert "headers" not in pub_db
        assert "leaked_secret_token" not in (setting.public_config_json or "")
        assert "sensitive_header" not in (setting.public_config_json or "")

        stored_secrets = get_secret_json("notification", setting.id)
        assert stored_secrets is not None
        assert stored_secrets["url"] == "https://example.com/hook?token=leaked_secret_token"
        assert stored_secrets["headers"] == {"X-Secret": "sensitive_header"}


# ---------------------------------------------------------------------------
# F04 Tests: Admin bearer token strength validation
# ---------------------------------------------------------------------------


def test_admin_token_strength_enforced_in_session_mode() -> None:
    # 1. session mode with too-short admin_token -> fails validation
    with pytest.raises(ValueError, match="CCM_ADMIN_TOKEN must contain at least 32 characters"):
        Settings(
            _env_file=None,
            env="production",
            auth_mode="session",
            owner_token=OWNER_TOKEN,
            admin_token=SecretStr("short"),
        )

    # 2. session mode with single-char admin_token -> fails validation
    with pytest.raises(ValueError, match="CCM_ADMIN_TOKEN must contain at least 32 characters"):
        Settings(
            _env_file=None,
            env="development",
            auth_mode="session",
            owner_token=OWNER_TOKEN,
            admin_token=SecretStr("x"),
        )

    # 3. session mode without admin_token configured -> allowed (admin bearer optional in session mode)
    s = Settings(
        _env_file=None,
        env="production",
        auth_mode="session",
        owner_token=OWNER_TOKEN,
        admin_token=None,
        allowed_host="testserver",
    )
    assert s.admin_token is None

    # 4. session mode with strong admin_token -> succeeds
    s2 = Settings(
        _env_file=None,
        env="production",
        auth_mode="session",
        owner_token=OWNER_TOKEN,
        admin_token=ADMIN_TOKEN,
        allowed_host="testserver",
    )
    assert s2.admin_token is not None


def test_admin_token_strength_enforced_in_token_mode() -> None:
    # 1. token mode with too-short token -> fails
    with pytest.raises(ValueError, match="CCM_ADMIN_TOKEN must contain at least 32 characters"):
        Settings(
            _env_file=None,
            env="production",
            auth_mode="token",
            admin_token=SecretStr("short-token"),
        )

    # 2. token mode without admin_token -> fails
    with pytest.raises(ValueError, match="CCM_ADMIN_TOKEN must contain at least 32 characters"):
        Settings(
            _env_file=None,
            env="production",
            auth_mode="token",
            admin_token=None,
        )

    # 3. token mode with strong token -> succeeds
    s = Settings(
        _env_file=None,
        env="production",
        auth_mode="token",
        admin_token=ADMIN_TOKEN,
        allowed_host="testserver",
    )
    assert s.admin_token is not None


# ---------------------------------------------------------------------------
# F05 Tests: Docker entrypoint & startup fail-safe
# ---------------------------------------------------------------------------


def _get_raw_popen():
    cur = subprocess.Popen
    if hasattr(cur, "__closure__") and cur.__closure__:
        for c in cur.__closure__:
            if isinstance(c.cell_contents, type) and c.cell_contents.__name__ == "Popen":
                return c.cell_contents
    return cur


def _run_docker_entrypoint(
    tmp_path: Path, configured_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    entrypoint_script = Path(__file__).resolve().parents[1] / "scripts" / "docker-entrypoint.sh"
    assert entrypoint_script.exists()

    sh_bin = shutil.which("sh") or r"C:\Program Files\Git\bin\sh.exe"
    if not Path(sh_bin).exists():
        pytest.skip("sh executable not found in test environment")

    script = entrypoint_script.read_text(encoding="utf-8")
    startup_marker = "mkdir -p /data/logs /data/backups"
    assert startup_marker in script
    harness = tmp_path / "docker-entrypoint-harness.sh"
    harness.write_text(
        script.split(startup_marker, maxsplit=1)[0] + "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )

    process_env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    process_env.update(configured_env)
    raw_popen = _get_raw_popen()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess, "Popen", raw_popen)
        return subprocess.run(
            [sh_bin, str(harness)],
            capture_output=True,
            text=True,
            env=process_env,
        )


@pytest.mark.parametrize("auth_mode", ["session", "token"])
def test_docker_entrypoint_accepts_supported_production_modes(
    tmp_path: Path, auth_mode: str
) -> None:
    result = _run_docker_entrypoint(
        tmp_path, {"CCM_ENV": "production", "CCM_AUTH_MODE": auth_mode}
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "configured_env",
    [
        {},
        {"CCM_ENV": ""},
        {"CCM_ENV": "production"},
        {"CCM_ENV": "production", "CCM_AUTH_MODE": ""},
        {"CCM_ENV": "development", "CCM_AUTH_MODE": "session"},
        {"CCM_ENV": "test", "CCM_AUTH_MODE": "session"},
        {"CCM_ENV": "production", "CCM_AUTH_MODE": "dev_bypass"},
        {"CCM_ENV": "production", "CCM_AUTH_MODE": "local"},
        {"CCM_ENV": "production", "CCM_AUTH_MODE": "unknown"},
        {"CCM_ENV": " production", "CCM_AUTH_MODE": "session"},
        {"CCM_ENV": "production", "CCM_AUTH_MODE": "TOKEN"},
    ],
)
def test_docker_entrypoint_rejects_unsafe_or_missing_modes(
    tmp_path: Path, configured_env: dict[str, str]
) -> None:
    result = _run_docker_entrypoint(tmp_path, configured_env)
    assert result.returncode != 0
    assert "FATAL:" in result.stderr


def test_production_environment_rejects_bypass() -> None:
    with pytest.raises(ValueError, match="forbidden in production"):
        Settings(
            _env_file=None,
            env="production",
            auth_mode="dev_bypass",
            owner_token=OWNER_TOKEN,
        )


def test_development_allows_bypass_only_with_explicit_configuration() -> None:
    # Explicit development + explicit dev_bypass is allowed
    s = Settings(
        _env_file=None,
        env="development",
        auth_mode="dev_bypass",
    )
    assert s.auth_mode == "dev_bypass"

    # Explicit development + session mode works
    s2 = Settings(
        _env_file=None,
        env="development",
        auth_mode="session",
        owner_token=OWNER_TOKEN,
    )
    assert s2.auth_mode == "session"
