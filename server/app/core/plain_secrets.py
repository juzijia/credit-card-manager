"""Plaintext secret vault stored at data/config/secrets.json.

Part of the plaintext-config migration: secrets live in a mode-600 file on the
data volume instead of encrypted DB columns. Writes are read-modify-replace;
the threading lock only guards this process (single-container deployment).
"""

import json
import os
import tempfile
import threading
from pathlib import Path

_LOCK = threading.Lock()


def _secrets_file() -> Path:
    configured = os.environ.get("CCM_SECRETS_FILE")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "data" / "config" / "secrets.json"


def _read_vault() -> dict:
    try:
        raw = _secrets_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return json.loads(raw) if raw.strip() else {}


def _write_vault(vault: dict) -> None:
    path = _secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".secrets-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(vault, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def get_secret(namespace: str, key: str) -> str | None:
    with _LOCK:
        value = _read_vault().get(namespace, {}).get(key)
    return value if isinstance(value, str) else None


def set_secret(namespace: str, key: str, value: str | None) -> None:
    with _LOCK:
        vault = _read_vault()
        scope = vault.setdefault(namespace, {})
        if value is None:
            scope.pop(key, None)
            if not scope:
                vault.pop(namespace, None)
        else:
            scope[key] = value
        _write_vault(vault)


def get_secret_json(namespace: str, key: str) -> dict | None:
    with _LOCK:
        value = _read_vault().get(namespace, {}).get(key)
    return value if isinstance(value, dict) else None


def set_secret_json(namespace: str, key: str, value: dict | None) -> None:
    with _LOCK:
        vault = _read_vault()
        scope = vault.setdefault(namespace, {})
        if value is None:
            scope.pop(key, None)
            if not scope:
                vault.pop(namespace, None)
        else:
            scope[key] = value
        _write_vault(vault)
