import json
from sqlalchemy.orm import Session
from app.services.system_settings import get_system_setting, set_system_setting

IGNORED_CARDS_KEY = "ignored_cards"


def get_ignored_cards(session: Session) -> list[dict[str, str]]:
    raw = get_system_setting(session, IGNORED_CARDS_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def is_card_ignored(session: Session, bank_code: str, last4: str, currency: str = "CNY") -> bool:
    """Return the Card-level ignore state; currency is accepted for API compatibility."""
    ignored = get_ignored_cards(session)
    return any(
        item.get("bank_code") == bank_code and item.get("last4") == last4
        for item in ignored
    )


def add_ignored_card(
    session: Session,
    bank_code: str,
    last4: str,
    bank_name: str | None = None,
    currency: str = "CNY",
    reason: str = "deleted_by_user",
    commit: bool = True,
) -> bool:
    ignored = get_ignored_cards(session)
    for item in ignored:
        if item.get("bank_code") == bank_code and item.get("last4") == last4:
            return False
    ignored.append({
        "bank_code": bank_code,
        "bank_name": bank_name or bank_code,
        "last4": last4,
        "currency": currency,
        "reason": reason,
    })
    set_system_setting(session, IGNORED_CARDS_KEY, json.dumps(ignored, ensure_ascii=False), commit=commit)
    return True


def remove_ignored_card(
    session: Session,
    bank_code: str,
    last4: str,
    currency: str = "CNY",
    commit: bool = True,
) -> bool:
    ignored = get_ignored_cards(session)
    # Unignore is Card-level: remove every legacy currency-specific entry.
    new_ignored = [
        item
        for item in ignored
        if not (
            item.get("bank_code") == bank_code
            and item.get("last4") == last4
        )
    ]
    if len(new_ignored) != len(ignored):
        set_system_setting(session, IGNORED_CARDS_KEY, json.dumps(new_ignored, ensure_ascii=False), commit=commit)
        return True
    return False
