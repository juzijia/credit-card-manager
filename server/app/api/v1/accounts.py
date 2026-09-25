import hashlib
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, SecretStr
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.security import require_owner_access
from app.db.session import get_session
from app.models.agent_token import AgentToken

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


class AgentTokenWrite(BaseModel):
    token: SecretStr | None = None


def _agent_token_enabled(session: Session) -> bool:
    return session.scalar(
        select(AgentToken.id).where(AgentToken.revoked.is_(False)).limit(1)
    ) is not None


@router.get("/agent-token", dependencies=[Depends(require_owner_access)])
def get_agent_token(session: SessionDep) -> dict[str, bool]:
    return {"enabled": _agent_token_enabled(session)}


@router.post("/agent-token/generate", dependencies=[Depends(require_owner_access)])
def generate_agent_token(response: Response) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {"token": secrets.token_urlsafe(32)}


@router.post("/agent-token/reveal", dependencies=[Depends(require_owner_access)])
def reveal_agent_token(
    request: Request, response: Response, session: SessionDep
) -> dict[str, str]:
    current = session.scalar(
        select(AgentToken)
        .where(AgentToken.revoked.is_(False))
        .order_by(AgentToken.created_at.desc(), AgentToken.id.desc())
        .limit(1)
    )
    if current is None:
        raise HTTPException(status_code=404, detail="Agent Token 尚未配置")
    if not current.secret_ciphertext:
        raise HTTPException(
            status_code=409,
            detail="当前 Agent Token 为旧版哈希存储，请生成新 Token 并保存一次后查看",
        )
    try:
        cleartext = decrypt_secret(
            current.secret_ciphertext, request.app.state.settings.master_key
        ).get_secret_value()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="已保存的 Agent Token 无法解密"
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    return {"token": cleartext}


@router.put(
    "/agent-token",
    dependencies=[Depends(require_owner_access)],
)
def save_agent_token(
    request: Request, payload: AgentTokenWrite, session: SessionDep
) -> dict[str, bool]:
    if "token" not in payload.model_fields_set:
        return {"enabled": _agent_token_enabled(session)}

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

    try:
        ciphertext = encrypt_secret(
            SecretStr(cleartext), request.app.state.settings.master_key
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    digest = hashlib.sha256(cleartext.encode()).hexdigest()
    matching = session.scalar(
        select(AgentToken).where(AgentToken.token_hash == digest)
    )
    if matching and matching.revoked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Agent token was previously disabled; generate a new token",
        )

    if matching:
        matching.name = "Agent Token"
        matching.scope = "agent"
        matching.secret_ciphertext = ciphertext
        current = matching
    else:
        current = AgentToken(
            name="Agent Token",
            scope="agent",
            token_hash=digest,
            secret_ciphertext=ciphertext,
        )
        session.add(current)
    session.flush()
    session.execute(
        update(AgentToken)
        .where(AgentToken.id != current.id, AgentToken.revoked.is_(False))
        .values(revoked=True)
    )
    session.commit()
    return {"enabled": True}
