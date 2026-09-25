import secrets
from collections import defaultdict
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import require_owner_access
from app.db.session import get_session
from app.services.session_service import (
    FALLBACK_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SUPPORTED_SESSION_COOKIE_NAMES,
    create_owner_session,
    revoke_owner_session,
)

router = APIRouter(prefix="/auth", tags=["auth"])
SessionDep = Annotated[Session, Depends(get_session)]

# Rate limiting storage: IP -> list of timestamps
_FAILED_ATTEMPTS: dict[str, list[datetime]] = defaultdict(list)
_MAX_FAILURES = 5
_WINDOW_SECONDS = 60


def _check_rate_limit(client_ip: str) -> None:
    now = datetime.now(UTC)
    attempts = [
        t
        for t in _FAILED_ATTEMPTS[client_ip]
        if (now - t).total_seconds() < _WINDOW_SECONDS
    ]
    _FAILED_ATTEMPTS[client_ip] = attempts
    if len(attempts) >= _MAX_FAILURES:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts. Please try again later. 认证失败次数过多，请稍后再试。",
        )


def _record_failure(client_ip: str) -> None:
    _FAILED_ATTEMPTS[client_ip].append(datetime.now(UTC))


class LoginPayload(BaseModel):
    owner_token: SecretStr = Field(min_length=1)


class SessionInfoResponse(BaseModel):
    authenticated: bool
    role: str | None = None
    csrf_token: str | None = None


def _is_secure_request(request: Request) -> bool:
    if request.url.is_secure:
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def _get_cookie_tokens(request: Request) -> list[str]:
    tokens: list[str] = []
    for name in SUPPORTED_SESSION_COOKIE_NAMES:
        token = request.cookies.get(name)
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _get_cookie_token(request: Request) -> str | None:
    tokens = _get_cookie_tokens(request)
    return tokens[0] if tokens else None


@router.post("/login")
def login_with_owner_token(
    payload: LoginPayload,
    request: Request,
    response: Response,
    session: SessionDep,
) -> dict[str, Any]:
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    settings: Settings = request.app.state.settings
    configured = settings.owner_token
    expected = configured.get_secret_value() if configured else ""
    supplied = payload.owner_token.get_secret_value()

    if not expected or not secrets.compare_digest(supplied, expected):
        _record_failure(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Owner Token 无效或不匹配",
        )

    _FAILED_ATTEMPTS.pop(client_ip, None)

    session_secret, csrf_token, _ = create_owner_session(
        session,
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )

    is_secure = _is_secure_request(request)
    cookie_key = SESSION_COOKIE_NAME if is_secure else FALLBACK_SESSION_COOKIE_NAME

    response.set_cookie(
        key=cookie_key,
        value=session_secret,
        max_age=settings.session_absolute_seconds,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        path="/",
    )

    return {
        "success": True,
        "csrf_token": csrf_token,
    }


@router.post("/logout", dependencies=[Depends(require_owner_access)])
def logout(
    request: Request,
    response: Response,
    session: SessionDep,
) -> dict[str, bool]:
    for cookie_token in _get_cookie_tokens(request):
        revoke_owner_session(session, cookie_token)

    for cookie_name in SUPPORTED_SESSION_COOKIE_NAMES:
        response.delete_cookie(
            key=cookie_name,
            path="/",
            httponly=True,
            samesite="lax",
        )
    return {"success": True}


@router.get("/session", response_model=SessionInfoResponse)
async def check_session(
    request: Request,
    session: SessionDep,
) -> dict[str, Any]:
    from app.services.session_service import get_valid_owner_session

    for cookie_token in _get_cookie_tokens(request):
        owner_sess = get_valid_owner_session(
            session,
            cookie_token,
            idle_seconds=request.app.state.settings.session_idle_seconds,
        )
        if owner_sess:
            return {
                "authenticated": True,
                "role": "owner",
                "csrf_token": owner_sess.csrf_token,
            }

    settings = request.app.state.settings
    if settings.auth_mode in ("dev_bypass", "local") and settings.env != "production":
        return {
            "authenticated": True,
            "role": "dev-admin",
            "csrf_token": "dev-bypass-csrf-token",
        }

    return {"authenticated": False, "role": None, "csrf_token": None}
