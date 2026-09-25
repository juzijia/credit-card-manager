from types import SimpleNamespace

import json
from pathlib import Path

import pytest

from app.services import backup


@pytest.fixture
def fake_obscure(monkeypatch):
    monkeypatch.setattr(
        backup, "obscure_password", lambda plaintext: "obscured" + plaintext[:1]
    )


def test_plain_env_has_no_crypt_remote(fake_obscure):
    env = backup.build_rclone_env(None, "https://dav.example/base/")
    assert not any("CRYPT" in key for key in env)
    assert env["RCLONE_CONFIG_CCM_WEBDAV_URL"] == "https://dav.example/base/"


def test_crypt_env_keeps_existing_remote(fake_obscure):
    env = backup.build_rclone_env("pw", "https://dav.example/base")
    assert env["RCLONE_CONFIG_CCM_WEBDAV_CRYPT_REMOTE"] == "CCM_WEBDAV:ccm-backups"
    assert env["RCLONE_CONFIG_CCM_WEBDAV_CRYPT_PASSWORD"] == "obscuredp"


def test_remote_target_switches_with_mode(fake_obscure):
    assert backup._remote_target(backup.build_rclone_env(None, "https://d/")) == (
        f"CCM_WEBDAV:{backup.PLAIN_REMOTE_DIR}/"
    )
    assert (
        backup._remote_target(backup.build_rclone_env("pw", "https://d/"))
        == "CCM_WEBDAV_CRYPT:"
    )


def _setting():
    return SimpleNamespace(webdav_url="https://dav.example/base/", webdav_username=None)


def test_remote_env_defaults_to_plain_without_vault_password(fake_obscure, monkeypatch):
    monkeypatch.setattr(backup, "get_secret", lambda *args: None)
    env = backup._remote_env(_setting())
    assert not any("CRYPT" in key for key in env)


def test_remote_env_uses_vault_password_when_present(fake_obscure, monkeypatch):
    secrets = {("backup", "crypt_password"): "vaultpw"}
    monkeypatch.setattr(backup, "get_secret", lambda *args: secrets.get(args))
    env = backup._remote_env(_setting())
    assert env["RCLONE_CONFIG_CCM_WEBDAV_CRYPT_PASSWORD"] == "obscuredv"


def test_remote_env_no_encryption_ignores_vault_password(fake_obscure, monkeypatch):
    monkeypatch.setattr(backup, "get_secret", lambda *args: "vaultpw")
    env = backup._remote_env(_setting(), encrypted=False)
    assert not any("CRYPT" in key for key in env)


def test_remote_env_requires_url(fake_obscure, monkeypatch):
    monkeypatch.setattr(backup, "get_secret", lambda *args: None)
    with pytest.raises(ValueError):
        backup._remote_env(SimpleNamespace(webdav_url=" ", webdav_username=None))


def test_remote_env_forced_crypt_requires_password(fake_obscure, monkeypatch):
    monkeypatch.setattr(backup, "get_secret", lambda *args: None)
    with pytest.raises(ValueError):
        backup._remote_env(_setting(), encrypted=True)


def _proc(stdout: str = ""):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


class FakeRclone:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    def __call__(self, args, env=None, stdin_input=None, timeout=60.0):
        op, rest = args[2], args[3:]
        if op == "mkdir":
            return _proc()
        if op == "lsjson":
            prefix = rest[1]
            items = [
                {"Path": key[len(prefix):], "Size": len(data), "IsDir": False}
                for key, data in self.files.items()
                if key.startswith(prefix) and "/" not in key[len(prefix):]
            ]
            return _proc(json.dumps(items))
        if op == "deletefile":
            if rest[0] not in self.files:
                raise RuntimeError("file not found")
            del self.files[rest[0]]
            return _proc()
        if op == "copyto":
            src, dst = rest
            source = Path(src)
            if source.is_file():
                self.files[dst] = source.read_bytes()
                return _proc()
            if src in self.files:
                Path(dst).write_bytes(self.files[src])
                return _proc()
            raise RuntimeError("directory not found")
        raise AssertionError(f"unexpected rclone op {op}")


def _wire_flows(monkeypatch, tmp_path):
    """Patch rclone/sqlite/session seams; return (fake, settings, setting)."""
    fake = FakeRclone()
    monkeypatch.setattr(backup, "run_rclone", fake)
    monkeypatch.setattr(backup, "get_secret", lambda *args: None)
    monkeypatch.setattr(
        backup, "sqlite_backup", lambda src, dst: Path(dst).write_bytes(b"payload")
    )
    db_file = tmp_path / "ccm.db"
    settings = SimpleNamespace(
        timezone="Asia/Shanghai",
        database_url=f"sqlite:///{db_file.as_posix()}",
    )
    setting = SimpleNamespace(
        webdav_url="https://dav.example/base/",
        webdav_username=None,
        retention_count=5,
        last_attempt_at=None,
        last_remote_backup_at=None,
        last_remote_status=None,
        last_error=None,
    )
    monkeypatch.setattr(
        backup, "get_backup_setting", lambda session, create=True: setting
    )
    session = SimpleNamespace(commit=lambda: None)
    return fake, settings, setting, session


@pytest.mark.parametrize(
    ("encrypted", "backup_password", "prefix"),
    [
        (False, None, f"CCM_WEBDAV:{backup.PLAIN_REMOTE_DIR}/"),
        (True, "pw", "CCM_WEBDAV_CRYPT:"),
    ],
)
def test_upload_list_restore_roundtrip_per_mode(
    fake_obscure, monkeypatch, tmp_path, encrypted, backup_password, prefix
):
    fake, settings, setting, session = _wire_flows(monkeypatch, tmp_path)
    result = backup.execute_backup(
        session,
        settings,
        encrypted=encrypted,
        backup_password=backup_password,
    )
    assert result["status"] == "SUCCESS"
    assert list(fake.files) == [prefix + result["filename"]]

    env = backup._remote_env(setting, encrypted=encrypted, crypt_password=backup_password)
    listed = backup.list_remote_backups(env, settings.timezone)
    assert [item["name"] for item in listed] == [result["filename"]]

    candidate = tmp_path / "restore" / "pending.sqlite"
    backup.download_restore_candidate(result["filename"], candidate, env)
    assert candidate.read_bytes() == b"payload"


def test_mode_switch_does_not_see_other_mode_backups(
    fake_obscure, monkeypatch, tmp_path
):
    fake, settings, setting, session = _wire_flows(monkeypatch, tmp_path)
    backup.execute_backup(session, settings, encrypted=True, backup_password="pw")
    plain_env = backup._remote_env(setting, encrypted=False)
    assert backup.list_remote_backups(plain_env, settings.timezone) == []
