#!/usr/bin/env python3
"""Credit Card Manager (CCM) HTTP tool with fixed LAN configuration."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 15.0
import os
DEFAULT_ENV_FILE = Path.home() / ".agents" / "skills" / "secrets" / "ccm.env"


class CCMError(RuntimeError):
    pass


def _config() -> tuple[str, str, str]:
    values: dict[str, str] = {}
    env_path_str = os.getenv("CCM_ENV_FILE")
    target_env_file = Path(env_path_str) if env_path_str else DEFAULT_ENV_FILE
    try:
        content = target_env_file.read_text(encoding="utf-8")
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip()
    except FileNotFoundError:
        pass

    base_url = os.getenv("CCM_BASE_URL") or values.get("CCM_BASE_URL", "")
    token = os.getenv("CCM_AGENT_TOKEN") or values.get("CCM_AGENT_TOKEN", "")
    host = os.getenv("CCM_HOST") or values.get("CCM_HOST", "")

    if not base_url or not token:
        raise CCMError("CCM_BASE_URL and CCM_AGENT_TOKEN are required (via env vars or ccm.env)")

    parsed = urllib.parse.urlparse(base_url)
    hostname = parsed.hostname or ""
    try:
        ip = ipaddress.ip_address(hostname)
        if not (ip.is_private or ip.is_loopback):
            raise CCMError("CCM_BASE_URL must point to a private LAN or loopback IP")
    except ValueError:
        if not (hostname == "localhost" or hostname.endswith(".local")):
            raise CCMError("CCM_BASE_URL must be a private IP or local hostname")

    return base_url.rstrip("/"), token, host


def _request(
    method: str,
    path: str,
    payload: Any | None = None,
    query: dict[str, Any] | None = None,
    headers_extra: dict[str, str] | None = None,
) -> Any:
    base_url, token, host = _config()
    clean_path = "/" + path.lstrip("/")

    full_url = base_url + clean_path
    if query:
        full_url += "?" + urllib.parse.urlencode(query)

    data: bytes | None = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if host:
        headers["Host"] = host
    if headers_extra:
        headers.update(headers_extra)

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(full_url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CCMError(f"HTTP {exc.code} {exc.reason}: {body}") from exc
    except Exception as exc:
        raise CCMError(f"Request failed: {exc}") from exc


def cmd_doctor() -> None:
    try:
        base_url, _, host = _config()
        req = urllib.request.Request(
            f"{base_url}/health",
            headers={"Host": host} if host else {},
        )
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            health = json.loads(resp.read().decode("utf-8"))

        cards = _request("GET", "/api/v1/cards")
        print(
            json.dumps(
                {
                    "status": "ok",
                    "health": health,
                    "cards_count": len(cards) if isinstance(cards, list) else 0,
                    "endpoint": base_url,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        sys.exit(1)


def cmd_get(path: str) -> None:
    res = _request("GET", path)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_post(path: str, payload_json: str | None = None, headers_json: str | None = None) -> None:
    payload = json.loads(payload_json) if payload_json else {}
    headers_extra = json.loads(headers_json) if headers_json else {}
    res = _request("POST", path, payload=payload, headers_extra=headers_extra)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="CCM API Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Verify connection and Agent Token auth")

    get_parser = subparsers.add_parser("get", help="Execute GET request on CCM path")
    get_parser.add_argument("--path", required=True, help="API route path, e.g. /api/v1/cards")

    post_parser = subparsers.add_parser("post", help="Execute POST request on CCM path")
    post_parser.add_argument("--path", required=True, help="API route path, e.g. /api/v1/imap/sync")
    post_parser.add_argument("--json", default=None, help="JSON body payload string")
    post_parser.add_argument("--headers", default=None, help="JSON headers dictionary string")

    args = parser.parse_args()
    if args.command == "doctor":
        cmd_doctor()
    elif args.command == "get":
        cmd_get(args.path)
    elif args.command == "post":
        cmd_post(args.path, args.json, args.headers)


if __name__ == "__main__":
    main()
