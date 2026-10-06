import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser
from starlette.types import Message, Scope

from app.api.v1 import imports as imports_api
from app.core.config import Settings
from app.db.base import Base
from app.main import IMPORT_REQUEST_BODY_LIMIT, RequestBodyLimitMiddleware, create_app
from app.parsers.eml import MAX_EML_BYTES


@pytest.fixture
def production_client(tmp_path: Path, admin_token: str) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        env="production",
        auth_mode="session",
        owner_token=SecretStr("synthetic-owner-token-" + "x" * 32),
        admin_token=SecretStr(admin_token),
        allowed_host="testserver",
        database_url=f"sqlite:///{(tmp_path / 'production.db').as_posix()}",
        error_log_path=None,
        background_tasks_enabled=False,
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as test_client:
        yield test_client


def test_oversized_multipart_is_rejected_before_form_parsing_or_side_effects(
    production_client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    parser_called = False
    endpoint_read = False
    business_call = False
    temporary_directory = False
    database_statements = 0
    original_parse = MultiPartParser.parse

    async def tracked_parse(self):  # pragma: no cover - must remain unreachable
        nonlocal parser_called
        parser_called = True
        return await original_parse(self)

    async def tracked_read(self, size: int = -1):  # pragma: no cover - unreachable
        nonlocal endpoint_read
        endpoint_read = True
        return b""

    def tracked_business(*_args, **_kwargs):  # pragma: no cover - unreachable
        nonlocal business_call
        business_call = True

    def tracked_temporary_directory(*_args, **_kwargs):  # pragma: no cover
        nonlocal temporary_directory
        temporary_directory = True

    def count_database_statement(*_args, **_kwargs) -> None:
        nonlocal database_statements
        database_statements += 1

    monkeypatch.setattr(MultiPartParser, "parse", tracked_parse)
    monkeypatch.setattr(UploadFile, "read", tracked_read)
    monkeypatch.setattr(imports_api, "parse_file", tracked_business)
    monkeypatch.setattr(imports_api, "import_eml_data", tracked_business)
    monkeypatch.setattr(imports_api, "import_pdf_bytes", tracked_business)
    monkeypatch.setattr(imports_api, "TemporaryDirectory", tracked_temporary_directory)
    event.listen(
        production_client.app.state.engine,
        "before_cursor_execute",
        count_database_statement,
    )
    try:
        for path, headers in (
            ("/api/v1/imports/inspect", {}),
            ("/api/v1/imports/eml", auth_headers),
            ("/api/v1/imports/file", auth_headers),
        ):
            response = production_client.post(
                path,
                headers=headers,
                files={
                    "file": (
                        "oversized.eml",
                        b"x" * (IMPORT_REQUEST_BODY_LIMIT + 1),
                    )
                },
            )
            assert response.status_code == 413
    finally:
        event.remove(
            production_client.app.state.engine,
            "before_cursor_execute",
            count_database_statement,
        )

    assert parser_called is False
    assert endpoint_read is False
    assert business_call is False
    assert temporary_directory is False
    assert database_statements == 0


def test_chunked_multipart_returns_413_through_fastapi_body_parser(
    production_client: TestClient, monkeypatch
) -> None:
    boundary = b"body-limit-boundary"
    prefix = (
        b"--"
        + boundary
        + b'\r\nContent-Disposition: form-data; name="file"; filename="large.eml"'
        + b"\r\nContent-Type: message/rfc822\r\n\r\n"
    )
    body = prefix + b"x" * (IMPORT_REQUEST_BODY_LIMIT + 128 * 1024) + b"\r\n--" + boundary + b"--\r\n"
    chunks = [body[index : index + 64 * 1024] for index in range(0, len(body), 64 * 1024)]
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    sent: list[Message] = []
    endpoint_read = False
    parser_file_bytes = 0
    original_write = UploadFile.write

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    async def tracked_read(self, size: int = -1):  # pragma: no cover - unreachable
        nonlocal endpoint_read
        endpoint_read = True
        return b""

    async def tracked_write(self, data: bytes) -> None:
        nonlocal parser_file_bytes
        parser_file_bytes += len(data)
        await original_write(self, data)

    monkeypatch.setattr(UploadFile, "read", tracked_read)
    monkeypatch.setattr(UploadFile, "write", tracked_write)
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/imports/eml",
        "raw_path": b"/api/v1/imports/eml",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"multipart/form-data; boundary=" + boundary),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(production_client.app(scope, receive, send))
    status = next(
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
    )

    assert status == 413
    assert endpoint_read is False
    assert parser_file_bytes <= IMPORT_REQUEST_BODY_LIMIT
    assert messages


def _run_asgi_body(
    *, chunks: list[bytes], content_length: bytes | None = None, limit: int = 5
) -> tuple[int, int, bool]:
    received_by_app = 0
    app_completed = False
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    async def downstream(
        _scope: Scope,
        receive_body: Callable[[], Awaitable[Message]],
        send_response: Callable[[Message], Awaitable[None]],
    ) -> None:
        nonlocal received_by_app, app_completed
        more_body = True
        while more_body:
            message = await receive_body()
            received_by_app += len(message.get("body", b""))
            more_body = message.get("more_body", False)
        app_completed = True
        await send_response({"type": "http.response.start", "status": 204, "headers": []})
        await send_response({"type": "http.response.body", "body": b""})

    headers = [] if content_length is None else [(b"content-length", content_length)]
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/imports/eml",
        "raw_path": b"/api/v1/imports/eml",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
        "root_path": "",
    }
    middleware = RequestBodyLimitMiddleware(
        downstream, paths=frozenset({"/api/v1/imports/eml"}), max_body_bytes=limit
    )
    asyncio.run(middleware(scope, receive, send))
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    return status, received_by_app, app_completed


def test_chunked_body_without_content_length_is_limited_by_actual_bytes() -> None:
    status, received_by_app, app_completed = _run_asgi_body(chunks=[b"abc", b"def"])

    assert status == 413
    assert received_by_app == 3
    assert app_completed is False


def test_small_declared_content_length_cannot_bypass_actual_byte_limit() -> None:
    status, received_by_app, app_completed = _run_asgi_body(
        chunks=[b"abc", b"def"], content_length=b"3"
    )

    assert status == 413
    assert received_by_app == 3
    assert app_completed is False


def test_request_body_limit_accepts_exact_boundary() -> None:
    status, received_by_app, app_completed = _run_asgi_body(
        chunks=[b"ab", b"cde"]
    )

    assert status == 204
    assert received_by_app == 5
    assert app_completed is True


def test_small_anonymous_upload_still_reaches_authentication(
    production_client: TestClient, monkeypatch
) -> None:
    parser_called = False
    original_parse = MultiPartParser.parse

    async def tracked_parse(self):
        nonlocal parser_called
        parser_called = True
        return await original_parse(self)

    monkeypatch.setattr(MultiPartParser, "parse", tracked_parse)
    response = production_client.post(
        "/api/v1/imports/eml", files={"file": ("small.eml", b"small")}
    )

    assert response.status_code == 401
    assert parser_called is True


def test_business_maximum_file_fits_inside_multipart_request_limit(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    monkeypatch.setattr(
        imports_api,
        "parse_file",
        lambda *_args: (
            SimpleNamespace(provider_id="synthetic"),
            [SimpleNamespace(currency="CNY", amount=1)],
        ),
    )
    response = client.post(
        "/api/v1/imports/inspect",
        headers=auth_headers,
        files={"file": ("maximum.pdf", b"x" * MAX_EML_BYTES)},
    )

    assert response.status_code == 200, response.text


def test_multipart_parser_parses_repeated_boundary_stream_safely() -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    async def run_scenario():
        headers = Headers({'content-type': 'multipart/form-data; boundary=test-boundary'})
        crlf = bytes([13, 10])
        parts = []
        for i in range(20):
            header = bytes('Content-Disposition: form-data; name=\'field%d\'' % i, 'latin1')
            part = b'--test-boundary' + crlf + header + crlf + crlf + b'val' + str(i).encode() + crlf
            parts.append(part)
        parts.append(b'--test-boundary--' + crlf)
        body = b''.join(parts)

        async def stream():
            for chunk in [body[i : i + 32] for i in range(0, len(body), 32)]:
                yield chunk

        parser = MultiPartParser(headers, stream())
        form = await parser.parse()
        assert len(form) == 20

    asyncio.run(run_scenario())

def test_anonymous_range_request_is_rejected_before_starlette_range_parser(
    client: TestClient, monkeypatch
) -> None:
    from fastapi.responses import FileResponse

    range_parser_called = False
    original_parse_range = FileResponse._parse_range_header

    def tracked_parse_range(*args, **kwargs):
        nonlocal range_parser_called
        range_parser_called = True
        return original_parse_range(*args, **kwargs)

    monkeypatch.setattr(FileResponse, "_parse_range_header", tracked_parse_range)

    # Malicious range payload with overlapping/many ranges
    malicious_range = "bytes=0-0," + ",".join(f"{i}-{i}" for i in range(200))

    for path in ["/", "/favicon.ico", "/static/favicon.svg"]:
        response = client.get(path, headers={"Range": malicious_range})
        assert response.status_code == 416
        assert "Range requests are not supported" in response.text

    assert range_parser_called is False


def test_urlencoded_form_is_rejected_before_starlette_form_parser(
    production_client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    from starlette.formparsers import FormParser

    form_parser_called = False
    original_form_parse = FormParser.parse

    async def tracked_form_parse(*args, **kwargs):
        nonlocal form_parser_called
        form_parser_called = True
        return await original_form_parse(*args, **kwargs)

    monkeypatch.setattr(FormParser, "parse", tracked_form_parse)

    # Malicious urlencoded body with numerous fields and large content
    fields = [f"key_{i}=value_{i}" for i in range(1000)]
    fields.append("huge_field=" + "A" * 50000)
    malicious_body = "&".join(fields)

    # 1. Reject on unauthenticated inspect endpoint
    res1 = production_client.post(
        "/api/v1/imports/inspect",
        content=malicious_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert res1.status_code == 415
    assert "application/x-www-form-urlencoded is not supported" in res1.text

    # 2. Reject on authenticated eml endpoint
    res2 = production_client.post(
        "/api/v1/imports/eml",
        content=malicious_body,
        headers={
            **auth_headers,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert res2.status_code == 415
    assert "application/x-www-form-urlencoded is not supported" in res2.text

    assert form_parser_called is False


def test_normal_static_resources_remain_accessible(client: TestClient) -> None:
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert "<!DOCTYPE html>" in res_index.text or "<html" in res_index.text

    res_favicon = client.get("/favicon.ico")
    assert res_favicon.status_code == 200

    res_svg = client.get("/static/favicon.svg")
    assert res_svg.status_code == 200


def test_normal_multipart_upload_and_auth_behavior_preserved(
    production_client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    monkeypatch.setattr(
        imports_api,
        "parse_file",
        lambda *_args: (
            SimpleNamespace(provider_id="synthetic"),
            [SimpleNamespace(currency="CNY", amount=1)],
        ),
    )

    # 1. Valid authenticated multipart upload on inspect endpoint succeeds
    res_inspect = production_client.post(
        "/api/v1/imports/inspect",
        headers=auth_headers,
        files={"file": ("test.pdf", b"pdf content")},
    )
    assert res_inspect.status_code == 200

    # 2. Anonymous multipart upload on inspect still reaches auth and returns 401
    res_anon_inspect = production_client.post(
        "/api/v1/imports/inspect",
        files={"file": ("test.pdf", b"pdf content")},
    )
    assert res_anon_inspect.status_code == 401

    # 2. Anonymous multipart upload on protected endpoint still reaches auth and returns 401
    res_anon = production_client.post(
        "/api/v1/imports/eml",
        files={"file": ("test.eml", b"eml content")},
    )
    assert res_anon.status_code == 401
