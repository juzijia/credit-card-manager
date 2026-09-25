from datetime import date, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.system_setting import SystemSetting
from app.services.statements import local_today


def get_system_setting(session: Session, key: str) -> str | None:
    try:
        row = session.scalar(select(SystemSetting).where(SystemSetting.key == key))
        return row.value if row else None
    except Exception:
        return None


def set_system_setting(session: Session, key: str, value: str, commit: bool = True) -> None:
    row = session.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if row:
        row.value = value
    else:
        row = SystemSetting(key=key, value=value)
        session.add(row)
    if commit:
        session.commit()


def ensure_history_cutoff_date(session: Session, today: date | None = None) -> date | None:
    """Initialize history_cutoff_date in database once during startup if missing."""
    try:
        raw = get_system_setting(session, "history_cutoff_date")
        if raw:
            return date.fromisoformat(raw)

        tz_name = (session.info.get("timezone") if session.info else None) or "Asia/Shanghai"
        ref_today = today or local_today(tz_name)
        cutoff = ref_today - timedelta(days=3)
        setting = SystemSetting(key="history_cutoff_date", value=cutoff.isoformat())
        session.add(setting)
        session.commit()
        return cutoff
    except Exception:
        session.rollback()
        return None


def get_history_cutoff_date(session: Session, today: date | None = None) -> date:
    """Read-only query for history_cutoff_date. Never performs database mutations on GET paths."""
    try:
        raw = get_system_setting(session, "history_cutoff_date")
        if raw:
            return date.fromisoformat(raw)
    except Exception:
        pass

    tz_name = (session.info.get("timezone") if session.info else None) or "Asia/Shanghai"
    ref_today = today or local_today(tz_name)
    return ref_today - timedelta(days=3)

