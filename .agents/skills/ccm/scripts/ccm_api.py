#!/usr/bin/env python3
"""Credit Card Manager (CCM) HTTP tool with fixed LAN configuration."""

from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 15.0
DEFAULT_ENV_FILE = Path.home() / ".agents" / "skills" / "secrets" / "ccm.env"
_MULTIPART_FIELD_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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
    query: list[tuple[str, str]] | None = None,
    headers_extra: dict[str, str] | None = None,
    raw_data: bytes | None = None,
    content_type: str | None = None,
) -> Any:
    if payload is not None and raw_data is not None:
        raise CCMError("JSON payload and raw request body are mutually exclusive")

    base_url, token, host = _config()
    clean_path = "/" + path.lstrip("/")
    full_url = base_url + clean_path
    if query:
        full_url += ("&" if "?" in full_url else "?") + urllib.parse.urlencode(query)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if host:
        headers["Host"] = host
    if headers_extra:
        headers.update(headers_extra)

    data = raw_data
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_data is not None and content_type:
        headers["Content-Type"] = content_type

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


def _query_pairs(values: list[str] | None) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise CCMError(f"Invalid query value {item!r}; expected KEY=VALUE")
        pairs.append((key, value))
    return pairs


def _multipart_file(path: str, field_name: str = "file") -> tuple[bytes, str]:
    if not _MULTIPART_FIELD_RE.fullmatch(field_name):
        raise CCMError("Multipart field name may contain only letters, digits, '.', '_' and '-'")

    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise CCMError(f"Upload file does not exist or is not a regular file: {file_path}")

    filename = file_path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    boundary = f"----ccm-{secrets.token_hex(16)}"
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {media_type}\r\n\r\n"
    ).encode("utf-8")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    return prefix + file_path.read_bytes() + suffix, f"multipart/form-data; boundary={boundary}"


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


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
        _print_json(
            {
                "status": "ok",
                "health": health,
                "cards_count": len(cards) if isinstance(cards, list) else 0,
                "endpoint": base_url,
            }
        )
    except Exception as exc:
        _print_json({"status": "error", "error": str(exc)})
        sys.exit(1)


def cmd_get(path: str) -> None:
    _print_json(_request("GET", path))


def cmd_json_request(
    method: str,
    path: str,
    payload_json: str | None = None,
    headers_json: str | None = None,
) -> None:
    if payload_json is None:
        payload = None if method.upper() == "DELETE" else {}
    else:
        payload = json.loads(payload_json)
    headers_extra = json.loads(headers_json) if headers_json else {}
    _print_json(_request(method, path, payload=payload, headers_extra=headers_extra))


def cmd_upload(
    path: str,
    file_path: str,
    query_values: list[str] | None = None,
    field_name: str = "file",
    headers_json: str | None = None,
) -> None:
    body, content_type = _multipart_file(file_path, field_name)
    headers_extra = json.loads(headers_json) if headers_json else {}
    _print_json(
        _request(
            "POST",
            path,
            query=_query_pairs(query_values),
            headers_extra=headers_extra,
            raw_data=body,
            content_type=content_type,
        )
    )


def _add_json_request_parser(subparsers: Any, command: str) -> None:
    request_parser = subparsers.add_parser(
        command,
        help=f"Execute {command.upper()} request on CCM path",
    )
    request_parser.add_argument("--path", required=True, help="API route path")
    request_parser.add_argument("--json", default=None, help="JSON body payload string")
    request_parser.add_argument("--headers", default=None, help="JSON headers dictionary string")


def main() -> None:
    parser = argparse.ArgumentParser(description="CCM API Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Verify connection and Agent Token auth")

    get_parser = subparsers.add_parser("get", help="Execute GET request on CCM path")
    get_parser.add_argument("--path", required=True, help="API route path, e.g. /api/v1/cards")

    for command in ("post", "patch", "put", "delete"):
        _add_json_request_parser(subparsers, command)

    upload_parser = subparsers.add_parser(
        "upload",
        help="POST one file as multipart/form-data to a CCM path",
    )
    upload_parser.add_argument("--path", required=True, help="API route path")
    upload_parser.add_argument("--file", required=True, help="Local file to upload")
    upload_parser.add_argument("--field", default="file", help="Multipart file field name")
    upload_parser.add_argument(
        "--query",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Query parameter; repeat for multiple values",
    )
    upload_parser.add_argument("--headers", default=None, help="JSON headers dictionary string")

    args = parser.parse_args()
    if args.command == "doctor":
        cmd_doctor()
    elif args.command == "get":
        cmd_get(args.path)
    elif args.command in {"post", "patch", "put", "delete"}:
        cmd_json_request(args.command.upper(), args.path, args.json, args.headers)
    elif args.command == "upload":
        cmd_upload(args.path, args.file, args.query, args.field, args.headers)


if __name__ == "__main__":
    main()
