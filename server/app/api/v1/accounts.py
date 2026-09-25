import secrets

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, SecretStr

from app.core.plain_secrets import get_secret, set_secret
from app.core.security import require_owner_access

router = APIRouter()

_AGENT_NAMESPACE = "agent"
_AGENT_KEY = "token"


class AgentTokenWrite(BaseModel):
    token: SecretStr | None = None


def _agent_token_enabled() -> bool:
    return bool(get_secret(_AGENT_NAMESPACE, _AGENT_KEY))


@router.get("/agent-token", dependencies=[Depends(require_owner_access)])
def get_agent_token() -> dict[str, bool]:
    return {"enabled": _agent_token_enabled()}


@router.post("/agent-token/generate", dependencies=[Depends(require_owner_access)])
def generate_agent_token(response: Response) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {"token": secrets.token_urlsafe(32)}


@router.post("/agent-token/reveal", dependencies=[Depends(require_owner_access)])
def reveal_agent_token(response: Response) -> dict[str, str]:
    token = get_secret(_AGENT_NAMESPACE, _AGENT_KEY)
    if not token:
        raise HTTPException(status_code=404, detail="Agent Token 尚未配置")
    response.headers["Cache-Control"] = "no-store"
    return {"token": token}


@router.put(
    "/agent-token",
    dependencies=[Depends(require_owner_access)],
)
def save_agent_token(payload: AgentTokenWrite) -> dict[str, bool]:
    if "token" not in payload.model_fields_set:
        return {"enabled": _agent_token_enabled()}

    cleartext = payload.token.get_secret_value() if payload.token is not None else ""
    if not cleartext:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Agent token is required; an enabled token can only be rotated",
        )

    if len(cleartext) < 32 or cleartext != cleartext.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Agent token must be at least 32 characters without surrounding whitespace",
        )

    # Vault write replaces the previous token, so saving is inherently a rotation.
    set_secret(_AGENT_NAMESPACE, _AGENT_KEY, cleartext)
    return {"enabled": True}
