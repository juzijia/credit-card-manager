import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.plain_secrets import get_secret, set_secret
from app.main import create_app


class PlaintextConfigTest(unittest.TestCase):
    def test_vault_and_agent_token_survive_app_restart(self):
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
                    first = "first-agent-token-" + "a" * 32
                    second = "second-agent-token-" + "b" * 32
                    self.assertEqual(
                        client.put("/api/v1/agent-token", json={"token": first}, headers=headers).status_code,
                        200,
                    )
                    status = client.get("/api/v1/agent-token")
                    self.assertEqual(status.json(), {"enabled": True})
                    self.assertNotIn(first, status.text)
                    reveal = client.post("/api/v1/agent-token/reveal", headers=headers)
                    self.assertEqual(reveal.json()["token"], first)
                    self.assertEqual(reveal.headers["cache-control"], "no-store")
                    self.assertEqual(client.get("/api/v1/cards", headers={"Authorization": f"Bearer {first}"}).status_code, 200)
                    self.assertEqual(
                        client.put("/api/v1/agent-token", json={"token": second}, headers=headers).status_code,
                        200,
                    )
                    client.cookies.clear()
                    self.assertEqual(client.get("/api/v1/cards", headers={"Authorization": f"Bearer {first}"}).status_code, 401)

                self.assertEqual(get_secret("agent", "token"), second)
                with TestClient(create_app(settings)) as client:
                    self.assertEqual(client.get("/api/v1/cards", headers={"Authorization": f"Bearer {second}"}).status_code, 200)
                self.assertNotIn(second, database.read_bytes().decode("latin1"))
                with closing(sqlite3.connect(database)) as conn:
                    self.assertEqual(conn.execute("select version_num from alembic_version").fetchone()[0], "0002")
                    self.assertNotIn("agent_tokens", {r[0] for r in conn.execute("select name from sqlite_master")})
                if os.name == "posix":
                    self.assertEqual(secrets_file.parent.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(secrets_file.stat().st_mode & 0o777, 0o600)
                set_secret("agent", "token", None)
                self.assertIsNone(get_secret("agent", "token"))


if __name__ == "__main__":
    unittest.main()
