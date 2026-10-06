"""Verify the isolation contract, without contacting real external services."""

import asyncio
from datetime import date
import imaplib
import os
from pathlib import Path
import smtplib
import socket
import subprocess
from urllib.request import urlopen

import httpx
import pytest
from sqlalchemy import func, select, text

from app.core.config import Settings, get_settings
from app.core.plain_secrets import get_secret, set_secret
from app.main import app
from app.models.statement import Statement


_collection_vault = Path(os.environ["CCM_SECRETS_FILE"])


def test_first_import_uses_pytest_temporary_state(tmp_path_factory):
    root = tmp_path_factory.getbasetemp()
    database = Path(app.state.engine.url.database)
    assert database.is_relative_to(root)
    assert Path(app.state.settings.error_log_path).is_relative_to(root)
    assert _collection_vault.is_relative_to(root)
    assert app.state.settings.background_tasks_enabled is False
    assert app.state.settings.auth_mode == "session"
    assert Settings.model_config["env_file"] is None
    with app.state.engine.connect() as connection:
        assert connection.scalar(text("select 1")) == 1
    assert database.is_file()


def test_lifespan_and_auth_are_real_without_background_jobs(ccm_app, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    spawned = []
    create_task = asyncio.create_task

    def guarded_task(coroutine, *args, **kwargs):
        if coroutine.__qualname__.split(".")[-1] in ("imap_loop", "reminder_loop", "backup_loop"):
            spawned.append(coroutine.__qualname__)
            coroutine.close()
            raise AssertionError("CCM background job was started")
        return create_task(coroutine, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", guarded_task)
    with TestClient(ccm_app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/api/v1/cards").status_code == 401
        login = client.post("/api/v1/auth/login", json={
            "owner_token": get_settings().owner_token.get_secret_value(),
        })
        assert login.status_code == 200
        assert client.get("/api/v1/cards").status_code == 200
    assert not spawned
    assert Path(ccm_app.state.engine.url.database).parent == tmp_path
    assert Path(ccm_app.state.settings.error_log_path).is_relative_to(tmp_path)


@pytest.mark.parametrize("value", ["first", "second"])
def test_database_and_vault_start_clean_for_each_test(ccm_client, tmp_path, value):
    assert Path(os.environ["CCM_SECRETS_FILE"]).is_relative_to(tmp_path)
    assert get_secret("bootstrap", "value") is None
    with ccm_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count(Statement.id))) == 0
        session.add(Statement(bank_code="BOC", statement_date=date(2026, 1, 1),
                              due_date=date(2026, 1, 20), total_due_minor=100))
        session.commit()
        assert session.scalar(select(func.count(Statement.id))) == 1
    set_secret("bootstrap", "value", value)
    assert get_secret("bootstrap", "value") == value
    assert Path(os.environ["CCM_SECRETS_FILE"]).is_file()


@pytest.mark.parametrize("operation", [
    lambda: socket.getaddrinfo("example.invalid", 443),
    lambda: socket.gethostbyname("example.invalid"),
    lambda: imaplib.IMAP4("example.invalid"),
    lambda: smtplib.SMTP("example.invalid", timeout=1),
    lambda: urlopen("https://example.invalid/", timeout=1),
    lambda: httpx.get("https://example.invalid/", timeout=1, trust_env=False),
    lambda: subprocess.run(["rclone", "lsjson", "CCM_WEBDAV:"], check=True),
    lambda: subprocess.run("docker compose config; rclone lsjson remote:", shell=True),
])
def test_external_io_fails_closed(operation):
    with pytest.raises(RuntimeError, match="pytest blocked external I/O"):
        operation()


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_unregistered_loopback_is_blocked(method):
    with socket.socket() as sock:
        with pytest.raises(RuntimeError, match="pytest blocked external I/O"):
            getattr(sock, method)(("127.0.0.1", 9))


def test_datagram_is_blocked():
    with socket.socket(type=socket.SOCK_DGRAM) as sock:
        with pytest.raises(RuntimeError, match="pytest blocked external I/O"):
            sock.sendto(b"synthetic", ("127.0.0.1", 9))
