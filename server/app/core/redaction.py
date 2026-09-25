from __future__ import annotations

import re


REDACTED = "[REDACTED]"

_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+")
_NAMED_SECRET_RE = re.compile(
    r"(?i)(password|passwd|authorization|api[_-]?key|access[_-]?token|"
    r"bot[_-]?token|token|secret|corpsecret|webhook[_-]?secret|bark[_-]?key)"
    r"(\s*[=:]\s*|[\"']\s*:\s*[\"'])"
    r"([^\s,;&\"'}]+)"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|token|key|secret|password|sign)=)[^&#\s]+"
)
_TELEGRAM_PATH_RE = re.compile(r"(?i)(/bot)[^/\s]+(/sendMessage)")
_CARD_NUMBER_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)")


def _mask_card_number(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return f"****{digits[-4:]}" if len(digits) >= 12 else match.group(0)


def redact_sensitive(value: object, *, limit: int = 500) -> str:
    """Return a bounded message safe for API responses, DB logs, and app logs."""
    text = " ".join(str(value).split())
    text = _AUTH_RE.sub(rf"\1{REDACTED}", text)
    text = _NAMED_SECRET_RE.sub(rf"\1\2{REDACTED}", text)
    text = _QUERY_SECRET_RE.sub(rf"\1{REDACTED}", text)
    text = _TELEGRAM_PATH_RE.sub(rf"\1{REDACTED}\2", text)
    text = _CARD_NUMBER_RE.sub(_mask_card_number, text)
    return text[:limit]
