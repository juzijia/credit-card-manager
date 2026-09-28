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
from app.core.plain_secrets import get_secret
from app.main import create_app


class BackupSettingsModeApiTest(unittest.TestCase):
    def test_password_lifecycle_and_cron_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "ccm.sqlite"
            with patch.dict(os.environ, {
                "CCM_DATABASE_URL": f"sqlite:///{database.as_posix()}",
                "CCM_SECRETS_FILE": str(root / "config" / "secrets.json"),
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

                    # Cron requires only the WebDAV URL; no backup password needed.
                    resp = client.put(
                        "/api/v1/backups/settings",
                        json={"webdav_url": "https://example.com/dav/backup/", "schedule_type": "cron"},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertFalse(resp.json()["backup_password_set"])
                    self.assertIsNone(get_secret("backup", "crypt_password"))

                    # Cron without URL is rejected.
                    resp = client.put(
                        "/api/v1/backups/settings",
                        json={"webdav_url": "", "schedule_type": "cron"},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 422)
                    resp = client.put(
                        "/api/v1/backups/settings",
                        json={"webdav_url": "https://example.com/dav/backup/"},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 200, resp.text)

                    # Non-empty password sets the vault entry.
                    resp = client.put(
                        "/api/v1/backups/settings",
                        json={"backup_password": "s3cret-pw"},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertTrue(resp.json()["backup_password_set"])

                    # Omitted password keeps the existing vault entry.
                    resp = client.put(
                        "/api/v1/backups/settings",
                        json={"retention_count": 7},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertTrue(resp.json()["backup_password_set"])
                    self.assertEqual(get_secret("backup", "crypt_password"), "s3cret-pw")

                    # Explicit empty string clears the vault entry and invalidates verification.
                    with closing(sqlite3.connect(database)) as conn:
                        conn.execute(
                            "update backup_settings set webdav_verified_at = '2026-01-01 00:00:00'"
                        )
                        conn.commit()
                    self.assertTrue(client.get("/api/v1/backups/settings").json()["connection_verified"])
                    resp = client.put(
                        "/api/v1/backups/settings",
                        json={"backup_password": ""},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertFalse(resp.json()["backup_password_set"])
                    self.assertFalse(resp.json()["connection_verified"])
                    self.assertIsNone(get_secret("backup", "crypt_password"))



    def test_allow_unencrypted_backup_setting_and_run_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "ccm.sqlite"
            with patch.dict(os.environ, {
                "CCM_DATABASE_URL": f"sqlite:///{database.as_posix()}",
                "CCM_SECRETS_FILE": str(root / "config" / "secrets.json"),
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

                    # Initial settings default to allow_unencrypted_backup=False
                    resp = client.get("/api/v1/backups/settings", headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    self.assertFalse(resp.json()["allow_unencrypted_backup"])
                    self.assertFalse(resp.json()["backup_password_set"])

                    # Set WebDAV URL
                    resp = client.put(
                        "/api/v1/backups/settings",
                        json={"webdav_url": "https://example.com/dav/backup/"},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 200)
                    self.assertFalse(resp.json()["allow_unencrypted_backup"])

                    # Running backup without password when unencrypted not allowed is rejected
                    run_resp = client.post("/api/v1/backups/run", headers=headers)
                    self.assertEqual(run_resp.status_code, 400)
                    self.assertIn("未配置备份密码", run_resp.json()["detail"])

                    # Testing connection without password when unencrypted not allowed is rejected
                    test_resp = client.post("/api/v1/backups/test", headers=headers)
                    self.assertEqual(test_resp.status_code, 400)
                    self.assertIn("未配置备份密码", test_resp.json()["detail"])

                    # Enable allow_unencrypted_backup
                    resp = client.put(
                        "/api/v1/backups/settings",
                        json={"allow_unencrypted_backup": True},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 200)
                    self.assertTrue(resp.json()["allow_unencrypted_backup"])

                    resp = client.put(
                        "/api/v1/backups/settings",
                        json={"retention_count": 7},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 200)
                    self.assertTrue(resp.json()["allow_unencrypted_backup"])

                    # Invalidate verification when toggling
                    with closing(sqlite3.connect(database)) as conn:
                        conn.execute(
                            "update backup_settings set webdav_verified_at = '2026-01-01 00:00:00'"
                        )
                        conn.commit()
                    self.assertTrue(client.get("/api/v1/backups/settings", headers=headers).json()["connection_verified"])

                    resp = client.put(
                        "/api/v1/backups/settings",
                        json={"allow_unencrypted_backup": False},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 200)
                    self.assertFalse(resp.json()["allow_unencrypted_backup"])
                    self.assertFalse(client.get("/api/v1/backups/settings", headers=headers).json()["connection_verified"])


if __name__ == "__main__":
    unittest.main()
