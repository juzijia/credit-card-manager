import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.mixins import utc_now
from app.models.owner_session import OwnerSession

SESSION_COOKIE_NAME = "__Host-ccm_session"
FALLBACK_SESSION_COOKIE_NAME = "ccm_session"
SUPPORTED_SESSION_COOKIE_NAMES = (SESSION_COOKIE_NAME, FALLBACK_SESSION_COOKIE_NAME)
CSRF_HEADER_NAME = "X-CSRF-Token"


def hash_session_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_owner_session(
    session: Session,
    *,
    idle_seconds: int = 86400,
    absolute_seconds: int = 2592000,
) -> tuple[str, str, OwnerSession]:
    """Generate session_secret and csrf_token, save only session_hash to DB."""
    session_secret = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = utc_now()

    db_session = OwnerSession(
        session_hash=hash_session_secret(session_secret),
        csrf_token=csrf_token,
        expires_at=now + timedelta(seconds=absolute_seconds),
        idle_expires_at=now + timedelta(seconds=idle_seconds),
        revoked=False,
        last_seen_at=now,
    )
    session.add(db_session)
    session.commit()
    session.refresh(db_session)
    return session_secret, csrf_token, db_session


def get_valid_owner_session(
    session: Session,
    session_secret: str,
    *,
    touch_interval_seconds: int = 60,
    idle_seconds: int = 86400,
) -> OwnerSession | None:
    if not session_secret:
        return None
    session_hash = hash_session_secret(session_secret)
    db_session = session.scalar(
        select(OwnerSession).where(
            OwnerSession.session_hash == session_hash,
            OwnerSession.revoked.is_(False),
        )
    )
    if db_session is None:
        return None

    now = utc_now()
    if now >= db_session.expires_at or now >= db_session.idle_expires_at:
        return None

    # Throttle touch write activity
    if (now - db_session.last_seen_at).total_seconds() > touch_interval_seconds:
        db_session.last_seen_at = now
        db_session.idle_expires_at = now + timedelta(seconds=idle_seconds)
        session.commit()

    return db_session


def revoke_owner_session(session: Session, session_secret: str) -> bool:
    if not session_secret:
        return False
    session_hash = hash_session_secret(session_secret)
    result = session.execute(
        update(OwnerSession)
        .where(OwnerSession.session_hash == session_hash, OwnerSession.revoked.is_(False))
        .values(revoked=True, updated_at=utc_now())
    )
    session.commit()
    return result.rowcount > 0

