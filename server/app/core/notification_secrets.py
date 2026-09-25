import re
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit


_CREDENTIAL_NAMES = frozenset(
    {
        "accesstoken",
        "apikey",
        "appsecret",
        "auth",
        "authorization",
        "authtoken",
        "barkkey",
        "bearer",
        "bottoken",
        "clientsecret",
        "corpsecret",
        "key",
        "password",
        "secret",
        "sign",
        "signature",
        "token",
        "webhooksecret",
    }
)


def _is_credential_name(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return normalized in _CREDENTIAL_NAMES


def is_credential_bearing_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    raw = value.strip()
    if "://" not in raw:
        return False
    try:
        parsed = urlsplit(raw)
        if parsed.username or parsed.password:
            return True
    except ValueError:
        return True

    if any(
        _is_credential_name(key)
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        return True

    segments = [segment for segment in unquote(parsed.path).split("/") if segment]
    for index, segment in enumerate(segments):
        if _is_credential_name(segment):
            return True
        if segment.casefold().startswith("bot") and len(segment) > 3:
            return True
        if segment.casefold() == "webhooks" and len(segments) > index + 2:
            return True

    fragment = parsed.fragment.lstrip("?#")
    return any(
        _is_credential_name(key)
        for key, _ in parse_qsl(fragment, keep_blank_values=True)
    )
