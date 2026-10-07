import secrets
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from app.models.owner_session import OwnerSession
from app.services.session_service import (
    CSRF_HEADER_NAME,
    FALLBACK_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    create_owner_session,
)


VALID_OWNER_TOKEN = secrets.token_urlsafe(32)
PRODUCTION_HOST = "testserver"


def _create_test_app(tmp_path: Path):
    settings = Settings(
        env="production",
        auth_mode="session",
        owner_token=SecretStr(VALID_OWNER_TOKEN),
        allowed_host=PRODUCTION_HOST,
        database_url=f"sqlite:///{(tmp_path / 'cookie_fallback.db').as_posix()}",
        background_tasks_enabled=False,
        error_log_path=None,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    return app, settings


def test_stale_primary_cookie_falls_through_to_valid_fallback_cookie(
    tmp_path: Path,
) -> None:
    app, settings = _create_test_app(tmp_path)

    with app.state.session_factory() as session:
        fallback_secret, csrf_token, session_row = create_owner_session(
            session,
            idle_seconds=settings.session_idle_seconds,
            absolute_seconds=settings.session_absolute_seconds,
        )
        fallback_session_id = session_row.id

    cookie_header = (
        f"{SESSION_COOKIE_NAME}=stale-session-secret; "
        f"{FALLBACK_SESSION_COOKIE_NAME}={fallback_secret}"
    )

    with TestClient(app, base_url="https://testserver") as client:
        session_res = client.get(
            "/api/v1/auth/session",
            headers={"Cookie": cookie_header},
        )
        assert session_res.status_code == 200
        assert session_res.json() == {
            "authenticated": True,
            "role": "owner",
            "csrf_token": csrf_token,
        }

        cards_res = client.get(
            "/api/v1/cards",
            headers={"Cookie": cookie_header},
        )
        assert cards_res.status_code == 200

        logout_res = client.post(
            "/api/v1/auth/logout",
            headers={"Cookie": cookie_header, CSRF_HEADER_NAME: csrf_token},
        )
        assert logout_res.status_code == 200

    with app.state.session_factory() as session:
        row = session.get(OwnerSession, fallback_session_id)
        assert row is not None
        assert row.revoked is True


def test_unsafe_request_can_match_csrf_to_second_valid_cookie(tmp_path: Path) -> None:
    app, settings = _create_test_app(tmp_path)

    with app.state.session_factory() as session:
        first_secret, _, _ = create_owner_session(
            session,
            idle_seconds=settings.session_idle_seconds,
            absolute_seconds=settings.session_absolute_seconds,
        )
        second_secret, second_csrf, _ = create_owner_session(
            session,
            idle_seconds=settings.session_idle_seconds,
            absolute_seconds=settings.session_absolute_seconds,
        )

    cookie_header = (
        f"{SESSION_COOKIE_NAME}={first_secret}; "
        f"{FALLBACK_SESSION_COOKIE_NAME}={second_secret}"
    )

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/cards",
            headers={"Cookie": cookie_header, CSRF_HEADER_NAME: second_csrf},
            json={
                "bank_code": "CMB",
                "bank_name": "CMB Bank",
                "display_name": "CMB Card",
                "last4": "1111",
                "currency": "CNY",
            },
        )
        assert response.status_code == 201
