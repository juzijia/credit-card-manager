import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.api.v1.auth import _FAILED_ATTEMPTS
from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.models.owner_session import OwnerSession
from app.services.session_service import (
    CSRF_HEADER_NAME,
    FALLBACK_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    hash_session_secret,
)

# Model the deployment contract: 32 cryptographically random bytes encoded URL-safe.
VALID_OWNER_TOKEN = secrets.token_urlsafe(32)
assert len(VALID_OWNER_TOKEN) >= 43
PRODUCTION_HOST = "testserver"


def test_production_fails_fast_with_dev_bypass_or_missing_owner_token() -> None:
    with pytest.raises(ValidationError):
        Settings(env="staging")

    with pytest.raises(ValidationError) as exc1:
        Settings(env="production", auth_mode="dev_bypass")
    assert "forbidden in production" in str(exc1.value)

    with pytest.raises(ValidationError) as exc2:
        Settings(env="production", auth_mode="session", owner_token=None)
    assert "generated from at least 32 random bytes" in str(exc2.value)

    with pytest.raises(ValidationError) as exc3:
        Settings(env="production", auth_mode="session", owner_token=SecretStr("short"))
    assert "generated from at least 32 random bytes" in str(exc3.value)

    # Session mode must have an Owner Token in every environment; development
    # bypass is represented explicitly by auth_mode=dev_bypass instead.
    with pytest.raises(ValidationError):
        Settings(env="development", auth_mode="session", owner_token=None)

    valid_settings = Settings(
        env="production",
        auth_mode="session",
        owner_token=SecretStr(VALID_OWNER_TOKEN),
        allowed_host=PRODUCTION_HOST,
    )
    assert valid_settings.env == "production"
    assert valid_settings.auth_mode == "session"


def test_production_allowed_host_validation() -> None:
    # Wildcards are strictly rejected
    for wildcard in ("*", "*.example.test"):
        with pytest.raises(ValidationError, match="one exact hostname without wildcards"):
            Settings(
                env="production",
                auth_mode="session",
                owner_token=SecretStr(VALID_OWNER_TOKEN),
                allowed_host=wildcard,
            )

    # Empty / None allowed_host is permitted in LAN mode
    for lan_host in (None, "", "  "):
        s = Settings(
            env="production",
            auth_mode="session",
            owner_token=SecretStr(VALID_OWNER_TOKEN),
            allowed_host=lan_host,
        )
        assert s.allowed_host is None

    # Exact hostname is preserved
    exact = Settings(
        env="production",
        auth_mode="session",
        owner_token=SecretStr(VALID_OWNER_TOKEN),
        allowed_host=PRODUCTION_HOST,
    )
    assert exact.allowed_host == PRODUCTION_HOST


def test_production_rejects_untrusted_host_without_redirect(tmp_path: Path) -> None:
    hostname = "cards.example.test"
    settings = Settings(
        env="production",
        auth_mode="session",
        owner_token=SecretStr(VALID_OWNER_TOKEN),
        allowed_host=hostname,
        database_url=f"sqlite:///{(tmp_path / 'host.db').as_posix()}",
        background_tasks_enabled=False,
        error_log_path=None,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)

    with TestClient(app, base_url=f"https://{hostname}") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health", headers={"Host": "127.0.0.1"}).status_code == 200
        assert client.get("/health", headers={"Host": "localhost"}).status_code == 200
        assert client.get("/health", headers={"Host": "192.168.1.100"}).status_code == 200
        assert client.get("/health", headers={"Host": "other.example.test"}).status_code == 400
        assert client.get("/health", headers={"Host": f"www.{hostname}"}).status_code == 400
        assert client.get("/health", headers={"Host": f"sub.{hostname}"}).status_code == 400


@pytest.mark.parametrize("environment", ["development", "test", "production"])
def test_only_openapi_json_is_exposed(environment: str, tmp_path: Path) -> None:
    settings = Settings(
        env=environment,
        auth_mode="session" if environment == "production" else "dev_bypass",
        owner_token=(
            SecretStr(VALID_OWNER_TOKEN) if environment == "production" else None
        ),
        allowed_host=PRODUCTION_HOST if environment == "production" else None,
        database_url=f"sqlite:///{(tmp_path / f'openapi_{environment}.db').as_posix()}",
        background_tasks_enabled=False,
        error_log_path=None,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)

    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc"):
            assert client.get(path).status_code == 404

    assert "/api/v1/due" in app.openapi()["paths"]


def test_session_auth_flow_login_csrf_and_logout(tmp_path: Path) -> None:
    settings = Settings(
        env="production",
        auth_mode="session",
        owner_token=SecretStr(VALID_OWNER_TOKEN),
        allowed_host=PRODUCTION_HOST,
        database_url=f"sqlite:///{(tmp_path / 'session_test.db').as_posix()}",
        background_tasks_enabled=False,
        error_log_path=None,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/v1/cards").status_code == 401

        wrong_login = client.post(
            "/api/v1/auth/login", json={"owner_token": "wrong-token"}
        )
        assert wrong_login.status_code == 401

        login_res = client.post(
            "/api/v1/auth/login", json={"owner_token": VALID_OWNER_TOKEN}
        )
        assert login_res.status_code == 200
        data = login_res.json()
        assert data["success"] is True
        csrf_token = data["csrf_token"]
        assert csrf_token is not None
        assert SESSION_COOKIE_NAME in client.cookies

        session_cookie = client.cookies[SESSION_COOKIE_NAME]
        with app.state.session_factory() as db:
            sess_row = db.query(OwnerSession).one()
            assert sess_row.session_hash == hash_session_secret(session_cookie)
            assert sess_row.csrf_token == csrf_token
            assert session_cookie not in sess_row.session_hash
            assert VALID_OWNER_TOKEN not in sess_row.session_hash

        cards_get = client.get("/api/v1/cards")
        assert cards_get.status_code == 200

        assert client.get("/api/v1/agent-token").json() == {"enabled": False}
        agent_token = client.post(
            "/api/v1/agent-token/generate",
            headers={CSRF_HEADER_NAME: csrf_token},
        ).json()["token"]
        assert client.put(
            "/api/v1/agent-token",
            headers={CSRF_HEADER_NAME: csrf_token},
            json={"token": agent_token},
        ).json() == {"enabled": True}
        assert client.post(
            "/api/v1/agent-token/reveal",
            headers={CSRF_HEADER_NAME: csrf_token},
        ).json() == {"token": agent_token}

        # Empty string clears token in new CCM
        cleared_res = client.put(
            "/api/v1/agent-token",
            headers={CSRF_HEADER_NAME: csrf_token},
            json={"token": ""},
        )
        assert cleared_res.status_code == 200
        assert cleared_res.json() == {"enabled": False}
        assert client.get("/api/v1/agent-token").json() == {"enabled": False}

        # Weak token is rejected
        rejected_weak = client.put(
            "/api/v1/agent-token",
            headers={CSRF_HEADER_NAME: csrf_token},
            json={"token": "too-short"},
        )
        assert rejected_weak.status_code == 422
        assert client.get("/api/v1/cards").status_code == 200

        unsafe_no_csrf = client.post(
            "/api/v1/cards",
            json={
                "bank_code": "CMB",
                "bank_name": "CMB Bank",
                "display_name": "CMB Card",
                "last4": "1111",
                "currency": "CNY",
            },
        )
        assert unsafe_no_csrf.status_code == 403
        assert "CSRF" in unsafe_no_csrf.text

        unsafe_with_csrf = client.post(
            "/api/v1/cards",
            headers={CSRF_HEADER_NAME: csrf_token},
            json={
                "bank_code": "CMB",
                "bank_name": "CMB Bank",
                "display_name": "CMB Card",
                "last4": "1111",
                "currency": "CNY",
            },
        )
        assert unsafe_with_csrf.status_code == 201

        sess_info = client.get("/api/v1/auth/session").json()
        assert sess_info["authenticated"] is True
        assert sess_info["role"] == "owner"
        assert sess_info["csrf_token"] == csrf_token

        # Logout is also an unsafe browser request and must be session-bound CSRF protected.
        logout_without_csrf = client.post("/api/v1/auth/logout")
        assert logout_without_csrf.status_code == 403
        assert client.get("/api/v1/cards").status_code == 200

        logout_res = client.post(
            "/api/v1/auth/logout", headers={CSRF_HEADER_NAME: csrf_token}
        )
        assert logout_res.status_code == 200
        assert client.get("/api/v1/cards").status_code == 401

        with app.state.session_factory() as db:
            assert db.query(OwnerSession).one().revoked is True


def test_session_idle_and_absolute_expiration(tmp_path: Path) -> None:
    settings = Settings(
        env="production",
        auth_mode="session",
        owner_token=SecretStr(VALID_OWNER_TOKEN),
        allowed_host=PRODUCTION_HOST,
        session_idle_seconds=1,
        session_absolute_seconds=2,
        database_url=f"sqlite:///{(tmp_path / 'expiry_test.db').as_posix()}",
        background_tasks_enabled=False,
        error_log_path=None,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)

    import time

    with TestClient(app, base_url="https://testserver") as client:
        login_res = client.post(
            "/api/v1/auth/login", json={"owner_token": VALID_OWNER_TOKEN}
        )
        assert login_res.status_code == 200
        assert client.get("/api/v1/cards").status_code == 200

        time.sleep(1.2)
        assert client.get("/api/v1/cards").status_code == 401


def test_login_rate_limiting(tmp_path: Path) -> None:
    _FAILED_ATTEMPTS.clear()
    settings = Settings(
        env="production",
        auth_mode="session",
        owner_token=SecretStr(VALID_OWNER_TOKEN),
        allowed_host=PRODUCTION_HOST,
        database_url=f"sqlite:///{(tmp_path / 'ratelimit_test.db').as_posix()}",
        background_tasks_enabled=False,
        error_log_path=None,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)

    with TestClient(app, base_url="https://testserver") as client:
        for _ in range(5):
            res = client.post(
                "/api/v1/auth/login", json={"owner_token": "wrong-token"}
            )
            assert res.status_code == 401

        res6 = client.post(
            "/api/v1/auth/login", json={"owner_token": "wrong-token"}
        )
        assert res6.status_code == 429
        assert "Too many failed" in res6.text


def test_owner_login_http_plain_sets_fallback_cookie_and_authenticates(tmp_path: Path) -> None:
    _FAILED_ATTEMPTS.clear()
    db_path = tmp_path / "http_test.db"
    settings = Settings(
        env="production",
        auth_mode="session",
        owner_token=SecretStr(VALID_OWNER_TOKEN),
        allowed_host=PRODUCTION_HOST,
        database_url=f"sqlite:///{db_path}",
        error_log_path=None,
        background_tasks_enabled=False,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)

    with TestClient(app, base_url="http://testserver") as client:
        wrong_login = client.post(
            "/api/v1/auth/login", json={"owner_token": "wrong-token"}
        )
        assert wrong_login.status_code == 401
        assert "Owner Token 无效或不匹配" in wrong_login.text

        login_res = client.post(
            "/api/v1/auth/login", json={"owner_token": VALID_OWNER_TOKEN}
        )
        assert login_res.status_code == 200
        assert FALLBACK_SESSION_COOKIE_NAME in client.cookies
        assert SESSION_COOKIE_NAME not in client.cookies

        session_res = client.get("/api/v1/auth/session")
        assert session_res.status_code == 200
        assert session_res.json()["authenticated"] is True
        assert session_res.json()["role"] == "owner"

        cards_res = client.get("/api/v1/cards")
        assert cards_res.status_code == 200
