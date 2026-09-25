import secrets
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.plain_secrets import get_secret
from app.services.session_service import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SUPPORTED_SESSION_COOKIE_NAMES,
    get_valid_owner_session,
)

@dataclass(frozen=True, slots=True)
class AccessContext:
    mode: Literal["session", "dev_bypass", "token", "local"]
    role: Literal["owner", "admin", "agent", "dev-admin"]
    session_id: str | None = None


_bearer = HTTPBearer(auto_error=False)


# Final Agent access decisions from the reviewed singleton-token audit. Route
# templates are exact, so every new private route is denied to Agents by default.
AGENT_ROUTE_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/cards"),
        ("GET", "/api/v1/cards/calendar-estimates"),
        ("GET", "/api/v1/cards/rules"),
        ("GET", "/api/v1/cards/{card_id}"),
        ("GET", "/api/v1/cards/{card_id}/interest-free-estimate"),
        ("PATCH", "/api/v1/cards/{card_id}"),
        ("GET", "/api/v1/catalog/stats"),
        ("GET", "/api/v1/catalog/banks"),
        ("GET", "/api/v1/catalog/products"),
        ("GET", "/api/v1/catalog/change-requests"),
        ("POST", "/api/v1/catalog/scan-runs"),
        ("GET", "/api/v1/catalog/scan-runs"),
        ("POST", "/api/v1/catalog/ingest"),
        ("GET", "/api/v1/statements"),
        ("GET", "/api/v1/statements/marked-paid"),
        ("GET", "/api/v1/statements/{statement_id}"),
        ("GET", "/api/v1/statements/{statement_id}/analysis"),
        ("POST", "/api/v1/statements/{statement_id}/mark-paid"),
        (
            "DELETE",
            "/api/v1/statements/{statement_id}/repayments/{repayment_id}",
        ),
        ("GET", "/api/v1/manual-bills"),
        ("GET", "/api/v1/manual-bills/current"),
        ("GET", "/api/v1/manual-bills/marked-paid"),
        ("POST", "/api/v1/manual-bills"),
        ("PUT", "/api/v1/manual-bills/{bill_id}"),
        ("DELETE", "/api/v1/manual-bills/{bill_id}"),
        ("POST", "/api/v1/manual-bills/{bill_id}/mark-paid"),
        ("POST", "/api/v1/manual-bills/{bill_id}/unmark-paid"),
        ("POST", "/api/v1/mortgage/save-bill"),
        ("GET", "/api/v1/mortgage/plans"),
        ("GET", "/api/v1/due"),
        ("GET", "/api/v1/dashboard"),
        ("GET", "/api/v1/analytics"),
        ("GET", "/api/v1/imports/providers"),
        ("GET", "/api/v1/imports/batches"),
        ("GET", "/api/v1/imports/batches/{batch_id}"),
        ("POST", "/api/v1/imports/inspect"),
        ("POST", "/api/v1/imports/eml"),
        ("POST", "/api/v1/imports/file"),
        ("GET", "/api/v1/imap/status"),
        ("GET", "/api/v1/imap/runs"),
        ("POST", "/api/v1/imap/sync"),
        ("POST", "/api/v1/imap/config"),
        ("GET", "/api/v1/transactions"),
        ("GET", "/api/v1/transactions/{transaction_id}/interest-free"),
        ("GET", "/api/v1/notifications/settings"),
        ("POST", "/api/v1/notifications/trigger"),
        ("GET", "/api/v1/notifications/logs"),
    }
)


def agent_route_is_allowed(request: Request) -> bool:
    route = request.scope.get("route")
    return (
        request.method.upper(),
        getattr(route, "path", None),
    ) in AGENT_ROUTE_ALLOWLIST


async def authenticate_access(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer)
    ] = None,
) -> AccessContext:
    settings = request.app.state.settings

    cookie_tokens: list[str] = []
    for name in SUPPORTED_SESSION_COOKIE_NAMES:
        cookie_token = request.cookies.get(name)
        if cookie_token and cookie_token not in cookie_tokens:
            cookie_tokens.append(cookie_token)

    if cookie_tokens:
        with request.app.state.session_factory() as session:
            valid_session_found = False
            for cookie_token in cookie_tokens:
                owner_sess = get_valid_owner_session(
                    session,
                    cookie_token,
                    idle_seconds=settings.session_idle_seconds,
                )
                if not owner_sess:
                    continue

                valid_session_found = True
                if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                    supplied_csrf = request.headers.get(CSRF_HEADER_NAME)
                    if not supplied_csrf or not secrets.compare_digest(
                        supplied_csrf, owner_sess.csrf_token
                    ):
                        continue

                return AccessContext(
                    mode="session",
                    role="owner",
                    session_id=owner_sess.id,
                )

            if valid_session_found and request.method in {
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
            }:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="CSRF 校验失败或令牌缺失",
                )

    supplied = credentials.credentials if credentials else ""
    if supplied:
        configured = settings.admin_token
        expected = configured.get_secret_value() if configured else ""
        if expected and secrets.compare_digest(supplied, expected):
            return AccessContext(mode="token", role="admin")

        vault_token = get_secret("agent", "token")
        if vault_token and secrets.compare_digest(supplied, vault_token):
            return AccessContext(mode="token", role="agent")

    if settings.auth_mode in ("dev_bypass", "local") and settings.env != "production" and not supplied:
        return AccessContext(mode="dev_bypass", role="dev-admin")

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未登录或身份认证已过期",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_agent_access(
    request: Request,
    context: Annotated[AccessContext, Depends(authenticate_access)],
) -> AccessContext:
    if context.role == "agent" and not agent_route_is_allowed(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent Token 无权执行此操作",
        )
    return context


require_agent_access.agent_access = "policy"


async def require_owner_access(
    context: Annotated[AccessContext, Depends(authenticate_access)],
) -> AccessContext:
    if context.role == "agent":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要 Owner 权限",
        )
    return context


require_owner_access.agent_access = "owner"
