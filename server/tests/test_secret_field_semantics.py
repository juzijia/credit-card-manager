import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.plain_secrets import get_secret
from app.main import create_app
from app.integrations.imap_sync import _sync_folder
from app.models.imap_state import ImapState
from app.schemas.imap import ImapConfigUpdateDTO, ImapConfigWriteDTO


class SecretFieldSemanticsApiTest(unittest.TestCase):
    def test_imap_initial_sync_months_zero_allowed(self):
        write_dto = ImapConfigWriteDTO(host="imap.example.com", username="user@example.com", initial_sync_months=0)
        self.assertEqual(write_dto.initial_sync_months, 0)
        update_dto = ImapConfigUpdateDTO(initial_sync_months=0)
        self.assertEqual(update_dto.initial_sync_months, 0)

        client = Mock()
        client.select_folder.return_value = {b"UIDVALIDITY": 1}
        client.search.return_value = []
        session = Mock()
        session.scalar.return_value = ImapState(account_key="test", mailbox="INBOX", uidvalidity=1, last_uid=0, pending_uids="[]")
        _sync_folder(client, session, "test", "INBOX", False, None, initial_sync_months=0)
        client.search.assert_called_once_with("ALL")

    def test_account_imap_notification_secret_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "ccm.sqlite"
            secrets_file = root / "config" / "secrets.json"
            with patch.dict(os.environ, {
                "CCM_DATABASE_URL": f"sqlite:///{database.as_posix()}",
                "CCM_SECRETS_FILE": str(secrets_file),
            }):
                cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
                command.upgrade(cfg, "head")
                settings = Settings(
                    database_url=f"sqlite:///{database.as_posix()}",
                    env="test",
                    auth_mode="session",
                    owner_token="o" * 43,
                    background_tasks_enabled=False,
                    error_log_path=None,
                )
                with TestClient(create_app(settings)) as client:
                    login = client.post("/api/v1/auth/login", json={"owner_token": "o" * 43})
                    self.assertEqual(login.status_code, 200, login.text)
                    headers = {"X-CSRF-Token": login.json()["csrf_token"]}

                    # 1. Agent token save, omit, clear
                    resp = client.put("/api/v1/agent-token", json={"token": "t" * 32}, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    self.assertTrue(resp.json()["enabled"])

                    resp = client.put("/api/v1/agent-token", json={}, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    self.assertTrue(resp.json()["enabled"])

                    resp = client.put("/api/v1/agent-token", json={"token": ""}, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    self.assertFalse(resp.json()["enabled"])
                    self.assertIsNone(get_secret("agent", "token"))
                    client.cookies.clear()
                    self.assertEqual(client.get("/api/v1/cards", headers={"Authorization": f"Bearer {'t' * 32}"}).status_code, 401)
                    headers = {"X-CSRF-Token": client.post("/api/v1/auth/login", json={"owner_token": "o" * 43}).json()["csrf_token"]}

                    # 2. IMAP save with 0, omit, clear
                    resp = client.post("/api/v1/imap/config", json={
                        "host": "imap.example.com", "port": 993, "username": "u@e.com",
                        "password": "pw", "initial_sync_months": 0
                    }, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    self.assertEqual(resp.json()["status"]["initial_sync_months"], 0)
                    self.assertTrue(resp.json()["status"]["password_set"])
                    self.assertNotIn("password", resp.json()["status"])

                    resp = client.post("/api/v1/imap/config", json={
                        "host": "imap.example.com", "port": 993, "username": "u@e.com", "initial_sync_months": 6
                    }, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    self.assertTrue(resp.json()["status"]["password_set"])
                    self.assertNotIn("password", resp.json()["status"])
                    self.assertEqual(resp.json()["status"]["initial_sync_months"], 6)

                    resp = client.post("/api/v1/imap/config", json={
                        "host": "imap.example.com", "port": 993, "username": "u@e.com", "password": ""
                    }, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    self.assertFalse(resp.json()["status"]["password_set"])
                    self.assertNotIn("password", resp.json()["status"])
                    self.assertNotIn("imap", json.loads(secrets_file.read_text(encoding="utf-8")))

                    # 3. Notification save, omit, clear, test
                    resp = client.post("/api/v1/notifications/settings", json={
                        "enabled": True, "provider": "telegram",
                        "provider_config": {"bot_token": "token123", "chat_id": "cid123"}
                    }, headers=headers)
                    self.assertEqual(resp.status_code, 200)

                    get_resp = client.get("/api/v1/notifications/settings", headers=headers)
                    self.assertEqual(get_resp.json()["setting"]["saved_provider"], "telegram")
                    self.assertEqual(get_resp.json()["setting"]["provider_config"]["bot_token"], "token123")

                    resp = client.post("/api/v1/notifications/settings", json={
                        "enabled": True, "provider": "telegram", "provider_config": {"chat_id": "cid456"}
                    }, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    get_resp = client.get("/api/v1/notifications/settings", headers=headers)
                    self.assertEqual(get_resp.json()["setting"]["provider_config"]["bot_token"], "token123")
                    self.assertEqual(get_resp.json()["setting"]["provider_config"]["chat_id"], "cid456")

                    resp = client.post("/api/v1/notifications/settings", json={
                        "enabled": True, "provider": "telegram", "provider_config": {"bot_token": ""}
                    }, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    get_resp = client.get("/api/v1/notifications/settings", headers=headers)
                    self.assertNotIn("bot_token", get_resp.json()["setting"]["provider_config"])
                    self.assertEqual(get_resp.json()["setting"]["provider_config"]["chat_id"], "cid456")

                    with patch("app.api.v1.notifications.test_notification", return_value=(True, "OK")) as sender:
                        test_resp = client.post("/api/v1/notifications/test", json={
                            "provider": "telegram", "provider_config": {"chat_id": ""}
                        }, headers=headers)
                        self.assertEqual(test_resp.status_code, 200)
                        self.assertNotIn("chat_id", sender.call_args.args[1])

                    resp = client.post("/api/v1/notifications/settings", json={
                        "enabled": False, "provider": "disabled", "provider_config": {}
                    }, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    saved = client.get("/api/v1/notifications/settings", headers=headers).json()["setting"]
                    self.assertEqual(saved["provider"], "disabled")
                    self.assertEqual(saved["saved_provider"], "telegram")
                    self.assertEqual(saved["provider_config"]["chat_id"], "cid456")

                    resp = client.post("/api/v1/notifications/settings", json={
                        "enabled": True, "provider": "telegram", "provider_config": {"chat_id": ""}
                    }, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    self.assertEqual(client.get("/api/v1/notifications/settings", headers=headers).json()["setting"]["provider_config"], {})
                    self.assertNotIn("notification", json.loads(secrets_file.read_text(encoding="utf-8")))

    def test_imap_secret_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "ccm.sqlite"
            secrets_file = root / "config" / "secrets.json"
            with patch.dict(os.environ, {
                "CCM_DATABASE_URL": f"sqlite:///{database.as_posix()}",
                "CCM_SECRETS_FILE": str(secrets_file),
            }):
                cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
                command.upgrade(cfg, "head")
                settings = Settings(
                    database_url=f"sqlite:///{database.as_posix()}",
                    env="test",
                    auth_mode="session",
                    owner_token="o" * 43,
                    background_tasks_enabled=False,
                    error_log_path=None,
                )
                with TestClient(create_app(settings)) as client:
                    login = client.post("/api/v1/auth/login", json={"owner_token": "o" * 43})
                    self.assertEqual(login.status_code, 200, login.text)
                    headers = {"X-CSRF-Token": login.json()["csrf_token"]}

                    # Set up Agent Token
                    agent_token = "a" * 32
                    resp = client.put("/api/v1/agent-token", json={"token": agent_token}, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    agent_headers = {"Authorization": f"Bearer {agent_token}"}

                    # Configure IMAP as Owner
                    raw_pw = "imap_secret_pass_12345"
                    resp = client.post("/api/v1/imap/config", json={
                        "host": "imap.example.com",
                        "port": 993,
                        "username": "owner@example.com",
                        "password": raw_pw,
                    }, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    self.assertTrue(resp.json()["status"]["configured"])
                    self.assertTrue(resp.json()["status"]["password_set"])
                    self.assertNotIn("password", resp.json()["status"])

                    # 1. Agent GET /imap/status -> 200, no cleartext password, configured/password_set state available
                    agent_status = client.get("/api/v1/imap/status", headers=agent_headers)
                    self.assertEqual(agent_status.status_code, 200)
                    status_data = agent_status.json()
                    self.assertTrue(status_data["configured"])
                    self.assertTrue(status_data["password_set"])
                    self.assertNotIn("password", status_data)
                    self.assertNotIn(raw_pw, agent_status.text)

                    # 2. Owner can reveal the configured IMAP password (with Cache-Control: no-store)
                    owner_reveal = client.post("/api/v1/imap/password/reveal", headers=headers)
                    self.assertEqual(owner_reveal.status_code, 200)
                    self.assertEqual(owner_reveal.json()["password"], raw_pw)
                    self.assertEqual(owner_reveal.headers.get("cache-control"), "no-store")

                    # 3. Agent calling the reveal endpoint -> 403
                    agent_reveal = client.post("/api/v1/imap/password/reveal", headers=agent_headers)
                    self.assertEqual(agent_reveal.status_code, 403)
                    self.assertNotIn(raw_pw, agent_reveal.text)

                    # 4. Invalid IMAP config resulting in validation error must not echo submitted cleartext password
                    invalid_pw = "should_not_leak_validation_pass_999"
                    invalid_resp = client.post("/api/v1/imap/config", json={
                        "port": 993,
                        "password": invalid_pw,
                    }, headers=headers)
                    self.assertEqual(invalid_resp.status_code, 422)
                    self.assertNotIn(invalid_pw, invalid_resp.text)
