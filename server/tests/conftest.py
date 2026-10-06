"""Import-time safety plus fresh state for each pytest test.

Use ccm_app/ccm_client for a migrated database and real lifespan. Existing
tests may keep their own temporary databases and explicitly mocked I/O.
"""

import os
from pathlib import Path
import secrets
import shlex
import socket
import subprocess
import sys
import threading
from http.server import HTTPServer
import logging

import pytest


_session_patch = pytest.MonkeyPatch()
_http_endpoints = set()
_owner_token = secrets.token_urlsafe(32)


def _configure_environment(root, patch):
    for key in list(os.environ):
        if key.startswith("CCM_"):
            patch.delenv(key)
    patch.setenv("CCM_ENV", "test")
    patch.setenv("CCM_AUTH_MODE", "session")
    patch.setenv("CCM_OWNER_TOKEN", _owner_token)
    patch.setenv("CCM_DATABASE_URL", f"sqlite:///{(root / 'ccm.sqlite').as_posix()}")
    patch.setenv("CCM_SECRETS_FILE", str(root / "vault" / "secrets.json"))
    patch.setenv("CCM_ERROR_LOG_PATH", str(root / "logs" / "errors.log"))
    patch.setenv("CCM_BACKGROUND_TASKS_ENABLED", "false")


def _install_io_guard(patch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo
    server_bind = HTTPServer.server_bind
    server_close = HTTPServer.server_close
    popen = subprocess.Popen
    socketpair = socket.socketpair
    ipc = threading.local()

    def check_address(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            # Windows implements stdlib socketpair with temporary loopback
            # sockets. Permit only that synchronous, thread-local operation.
            if getattr(ipc, "creating_pair", False) and address[0] in ("127.0.0.1", "::1"):
                return
            if tuple(address[:2]) not in _http_endpoints:
                raise RuntimeError("pytest blocked external I/O: socket")

    def guarded_connect(sock, address):
        check_address(sock, address)
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        check_address(sock, address)
        return connect_ex(sock, address)

    def guarded_socketpair(*args, **kwargs):
        ipc.creating_pair = True
        try:
            return socketpair(*args, **kwargs)
        finally:
            ipc.creating_pair = False

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if (host, port) not in _http_endpoints:
            raise RuntimeError("pytest blocked external I/O: DNS")
        return getaddrinfo(host, port, *args, **kwargs)

    def blocked_datagram(*args, **kwargs):
        raise RuntimeError("pytest blocked external I/O: datagram")

    def blocked_dns(*args, **kwargs):
        raise RuntimeError("pytest blocked external I/O: DNS")

    def guarded_gethostbyaddr(address):
        if address != "127.0.0.1":
            return blocked_dns()
        return ("localhost", [], ["127.0.0.1"])

    def guarded_server_bind(server):
        # Only a test-created HTTP server on an OS-assigned loopback port.
        if server.server_address != ("127.0.0.1", 0):
            raise RuntimeError("pytest HTTP server must use ephemeral loopback")
        server_bind(server)
        _http_endpoints.add(server.server_address)

    def guarded_server_close(server):
        _http_endpoints.discard(server.server_address)
        return server_close(server)

    def guarded_popen(args, *positional, **kwargs):
        command = shlex.split(args) if isinstance(args, str) else list(args)
        command = [os.fsdecode(arg) for arg in command]
        executable = Path(command[0]).name if command else ""
        compose_config = command == ["docker", "compose", "config"]
        node_calculation = (
            executable in ("node", "node.exe")
            and len(command) == 4
            and command[1] == "-e"
            and not kwargs.get("shell")
        )
        if kwargs.get("executable") or not (compose_config or node_calculation):
            raise RuntimeError("pytest blocked external I/O: subprocess")
        # Preserve subprocess semantics, including existing shell=True tests.
        return popen(args, *positional, **kwargs)

    patch.setattr(socket.socket, "connect", guarded_connect)
    patch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    patch.setattr(socket, "socketpair", guarded_socketpair)
    patch.setattr(socket.socket, "sendto", blocked_datagram)
    if hasattr(socket.socket, "sendmsg"):
        patch.setattr(socket.socket, "sendmsg", blocked_datagram)
    patch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    patch.setattr(socket, "gethostbyname", blocked_dns)
    patch.setattr(socket, "gethostbyname_ex", blocked_dns)
    patch.setattr(socket, "gethostbyaddr", guarded_gethostbyaddr)
    patch.setattr(HTTPServer, "server_bind", guarded_server_bind)
    patch.setattr(HTTPServer, "server_close", guarded_server_close)
    patch.setattr(subprocess, "Popen", guarded_popen)


def pytest_sessionstart(session):
    if "app.main" in sys.modules or "app.core.config" in sys.modules:
        raise pytest.UsageError("CCM must not be imported before test bootstrap")
    # pytest 8.4.2 initializes this factory before collection; fixtures are
    # too late to protect the module-level app = create_app().
    root = session.config._tmp_path_factory.mktemp("ccm-import")
    _configure_environment(root, _session_patch)
    _install_io_guard(_session_patch)
    from app.core.config import Settings, get_settings

    _session_patch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()


def pytest_unconfigure(config):
    if "app.main" in sys.modules:
        sys.modules["app.main"].app.state.engine.dispose()
    _session_patch.undo()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    from app.core.config import get_settings

    _configure_environment(tmp_path, monkeypatch)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
        for handler in list(logging.getLogger().handlers):
            if getattr(handler, "ccm_error_log", None):
                logging.getLogger().removeHandler(handler)
                handler.close()


@pytest.fixture
def ccm_app(_isolated_state):
    from alembic import command
    from alembic.config import Config
    from app.main import create_app

    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    application = create_app()
    try:
        yield application
    finally:
        application.state.engine.dispose()


@pytest.fixture
def ccm_client(ccm_app):
    from fastapi.testclient import TestClient

    with TestClient(ccm_app) as client:
        yield client


@pytest.fixture
def admin_token() -> str:
    return "test-admin-token-that-is-at-least-32-characters"


@pytest.fixture
def client(tmp_path, admin_token):
    from fastapi.testclient import TestClient
    from app.core.config import Settings
    from app.db.base import Base
    from app.main import create_app

    settings = Settings(
        _env_file=None,
        env="test",
        auth_mode="token",
        admin_token=admin_token,
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        timezone="Asia/Shanghai",
        error_log_path=None,
        background_tasks_enabled=False,
    )
    application = create_app(settings)
    Base.metadata.create_all(application.state.engine)
    try:
        with TestClient(application) as test_client:
            yield test_client
    finally:
        application.state.engine.dispose()


@pytest.fixture
def auth_headers(admin_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}
