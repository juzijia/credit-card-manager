from datetime import datetime
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import patch
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from app.core.config import Settings
from app.main import create_app
from app.models.backup import BackupSetting
from app.services import backup
from app.services.backup import (
    BackupBusyError,
    apply_restore_offline,
    backup_due,
    build_rclone_env,
    cleanup_backups,
    execute_backup,
    get_backup_paths,
    obscure_password,
    prepare_pending_restore,
    run_rclone,
    validate_backup_name,
)
from app.services.database_maintenance import sqlite_backup


@pytest.fixture
def backup_app(tmp_path: Path):
    database = tmp_path / "test.db"
    secrets_file = tmp_path / "secrets.json"
    settings = Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        admin_token="01234567890123456789012345678901Fill",
        auth_mode="token",
        env="test",
        background_tasks_enabled=False,
    )
    with patch.dict(
        os.environ,
        {
            "CCM_DATABASE_URL": settings.database_url,
            "CCM_SECRETS_FILE": str(secrets_file),
        },
    ):
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        app = create_app(settings)
        app.state.graceful_shutdown = lambda: None
        with TestClient(
            app, headers={"Authorization": "Bearer 01234567890123456789012345678901Fill"}
        ) as client:
            yield settings, client, database


def configure(client: TestClient, **overrides):
    payload = {
        "backup_password": "BackupPassword!",
        "webdav_url": "https://dav.example.test/root/",
        "webdav_username": "owner",
        "webdav_password": "DavPassword!",
    }
    payload.update(overrides)
    response = client.put("/api/v1/backups/settings", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def completed(args, stdout=""):
    return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")


def fake_obscure_and_empty_list(args, **_kwargs):
    if args[:2] == ["obscure", "-"]:
        return completed(args, "obscured\n")
    if "lsjson" in args:
        return completed(args, "[]")
    return completed(args)


def _write_marker(paths, name="ccm-20260909-030000.sqlite"):
    paths.marker_file.write_text(
        json.dumps({"backup_name": name, "requested_at": "2026-09-09T03:00:00+08:00"}),
        encoding="utf-8",
    )


def test_rclone_runner_and_obscure_keep_secrets_out_of_argv():
    with patch("subprocess.run") as process:
        process.return_value = completed(["rclone"], "rclone v1.75.1\n")
        assert run_rclone(["version"], env={"CUSTOM": "ok"}).stdout.startswith("rclone")
        kwargs = process.call_args.kwargs
        assert kwargs["shell"] is False
        assert kwargs["env"]["CUSTOM"] == "ok"

        process.side_effect = subprocess.TimeoutExpired(["rclone"], 5)
        with pytest.raises(RuntimeError, match="timed out"):
            run_rclone(["copyto"], timeout=5)

        process.side_effect = None
        process.return_value = subprocess.CompletedProcess(["rclone"], 1, "", "failed")
        with pytest.raises(RuntimeError, match="failed"):
            run_rclone(["copyto"])

    with patch("app.services.backup.run_rclone") as runner:
        runner.return_value = completed([], "hidden\n")
        assert obscure_password("plain-secret") == "hidden"
        args = runner.call_args.args[0]
        assert args == ["obscure", "-"]
        assert "plain-secret" not in args
        assert runner.call_args.kwargs["stdin_input"] == b"plain-secret\n"


def test_webdav_only_backup_creates_one_remote_object_and_cleans_staging(backup_app):
    settings, client, _ = backup_app
    configure(client)
    paths = get_backup_paths(settings.database_url)
    calls = []

    def runner(args, **_kwargs):
        calls.append(args)
        return fake_obscure_and_empty_list(args)

    with patch("app.services.backup.run_rclone", side_effect=runner):
        response = client.post("/api/v1/backups/run")
        env = build_rclone_env("backup", "https://dav.example.test")
    assert response.status_code == 200
    assert response.json()["status"] == "SUCCESS"
    uploads = [args for args in calls if "copyto" in args]
    assert len(uploads) == 1
    assert uploads[0][-1].startswith("CCM_WEBDAV_CRYPT:ccm-")
    assert not list(paths.staging_dir.glob("*.sqlite"))
    assert not (paths.database_file.parent / "backups" / "local").exists()
    assert not any("LOCAL" in key for key in env)


def test_failed_upload_cleans_plaintext_staging(backup_app):
    settings, client, _ = backup_app
    configure(client)
    paths = get_backup_paths(settings.database_url)

    def runner(args, **_kwargs):
        if args[:2] == ["obscure", "-"]:
            return completed(args, "obscured")
        if "copyto" in args:
            raise RuntimeError("remote failed")
        return completed(args)

    with patch("app.services.backup.run_rclone", side_effect=runner):
        assert client.post("/api/v1/backups/run").status_code == 400
    assert not list(paths.staging_dir.glob("*.sqlite"))


def test_list_and_retention_use_remote_as_truth_and_ignore_unrelated_files(backup_app):
    settings, client, _ = backup_app
    configure(client, retention_count=1)
    remote_items = [
        {"Path": "notes.txt", "Size": 1, "ModTime": "2026-09-01T00:00:00Z"},
        {"Path": "ccm-20260908-030000.sqlite", "Size": 100, "ModTime": "2026-09-08T03:00:00Z"},
        {"Path": "ccm-20260909-030000.sqlite", "Size": 200, "ModTime": "2026-09-09T03:00:00Z"},
        {"Path": "ccm-../../evil.sqlite", "Size": 1, "ModTime": "2026-09-01T00:00:00Z"},
    ]
    deleted = []

    def runner(args, **_kwargs):
        if args[:2] == ["obscure", "-"]:
            return completed(args, "obscured")
        if "lsjson" in args:
            return completed(args, json.dumps(remote_items))
        if "deletefile" in args:
            deleted.append(args[-1])
            return completed(args)
        return completed(args)

    with patch("app.services.backup.run_rclone", side_effect=runner):
        listed = client.get("/api/v1/backups")
        cleaned = client.post("/api/v1/backups/cleanup")
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()] == [
        "ccm-20260909-030000.sqlite",
        "ccm-20260908-030000.sqlite",
    ]
    assert cleaned.json() == {"kept": 1, "deleted": 1, "failed": 0}
    assert deleted == ["CCM_WEBDAV_CRYPT:ccm-20260908-030000.sqlite"]


def test_cleanup_failure_does_not_flip_successful_upload(backup_app):
    _, client, _ = backup_app
    configure(client)

    def runner(args, **_kwargs):
        if args[:2] == ["obscure", "-"]:
            return completed(args, "obscured")
        if "lsjson" in args:
            raise RuntimeError("listing failed")
        return completed(args)

    with patch("app.services.backup.run_rclone", side_effect=runner):
        response = client.post("/api/v1/backups/run")
    assert response.status_code == 200
    assert response.json()["status"] == "SUCCESS"
    assert response.json()["warning"]
    assert client.get("/api/v1/backups/settings").json()["last_backup_status"] == "SUCCESS"


def test_list_rclone_error_is_reported(backup_app):
    _, client, _ = backup_app
    configure(client)
    with patch("app.services.backup.run_rclone", side_effect=RuntimeError("unavailable")):
        response = client.get("/api/v1/backups")
    assert response.status_code == 400


def test_schedule_due_off_catchup_and_failed_run_waits_for_next_cycle(backup_app):
    settings, client, _ = backup_app
    configure(client, schedule_type="cron", cron_expr="0 3 * * *")
    now = datetime(2026, 9, 9, 4, 0, tzinfo=ZoneInfo(settings.timezone))
    with client.app.state.session_factory() as session:
        setting = session.get(BackupSetting, "default")
        assert backup_due(setting, now, settings.timezone) is True
        setting.last_attempt_at = datetime(2026, 9, 9, 3, 30)
        session.commit()
        assert backup_due(setting, now, settings.timezone) is False
        setting.schedule_type = "off"
        session.commit()
        assert backup_due(setting, now, settings.timezone) is False

    client.put("/api/v1/backups/settings", json={"schedule_type": "cron"})
    with patch("app.services.backup.run_rclone", side_effect=RuntimeError("remote failed")):
        with client.app.state.session_factory() as session:
            with pytest.raises(RuntimeError):
                execute_backup(session, settings)
            setting = session.get(BackupSetting, "default")
            attempted = setting.last_attempt_at.replace(tzinfo=ZoneInfo(settings.timezone))
            assert backup_due(setting, attempted, settings.timezone) is False


def test_manual_cleanup_and_restore_share_one_process_lock(backup_app):
    settings, client, _ = backup_app
    configure(client)
    with client.app.state.session_factory() as session:
        backup.BACKUP_OPERATION_LOCK.acquire()
        try:
            with pytest.raises(BackupBusyError):
                execute_backup(session, settings)
            with pytest.raises(BackupBusyError):
                cleanup_backups(session, settings)
            with pytest.raises(BackupBusyError):
                prepare_pending_restore(
                    session, settings, "ccm-20260909-030000.sqlite"
                )
        finally:
            backup.BACKUP_OPERATION_LOCK.release()


@pytest.mark.parametrize(
    "name",
    ["../x", "..%2Fx", "ccm/evil.sqlite", r"ccm\evil.sqlite", "notes.sqlite", "ccm-20260909-030000.sqlite/.."],
)
def test_restore_name_validation_rejects_traversal(name):
    with pytest.raises(ValueError):
        validate_backup_name(name)


def test_restore_api_rejects_encoded_traversal(backup_app):
    _, client, _ = backup_app
    configure(client)
    response = client.post("/api/v1/backups/..%5Cevil/restore")
    assert response.status_code == 400


def test_restore_api_only_prepares_candidate_then_returns_202(backup_app):
    settings, client, database = backup_app
    configure(client)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE restore_probe(value TEXT)")
        connection.execute("INSERT INTO restore_probe VALUES ('current')")
        connection.commit()
    candidate = database.parent / "candidate.db"
    sqlite_backup(database, candidate)
    with closing(sqlite3.connect(candidate)) as connection:
        connection.execute("UPDATE restore_probe SET value='restored'")
        connection.commit()
    shutdowns = []
    client.app.state.graceful_shutdown = lambda: shutdowns.append(True)

    def runner(args, **_kwargs):
        if args[:2] == ["obscure", "-"]:
            return completed(args, "obscured")
        if "copyto" in args:
            Path(args[-1]).write_bytes(candidate.read_bytes())
        return completed(args)

    with patch("app.services.backup.run_rclone", side_effect=runner):
        response = client.post(
            "/api/v1/backups/ccm-20260909-030000.sqlite/restore"
        )
    assert response.status_code == 202
    assert shutdowns == [True]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT value FROM restore_probe").fetchone()[0] == "current"
    paths = get_backup_paths(settings.database_url)
    manifest = json.loads(paths.marker_file.read_text(encoding="utf-8"))
    assert set(manifest) == {"backup_name", "requested_at"}
    assert "Password" not in paths.marker_file.read_text(encoding="utf-8")
    assert paths.pending_file.exists()


def test_invalid_restore_candidate_leaves_production_untouched(backup_app):
    settings, client, database = backup_app
    configure(client)
    original = database.read_bytes()

    def runner(args, **_kwargs):
        if args[:2] == ["obscure", "-"]:
            return completed(args, "obscured")
        if "copyto" in args:
            Path(args[-1]).write_text("not sqlite", encoding="utf-8")
        return completed(args)

    with patch("app.services.backup.run_rclone", side_effect=runner):
        response = client.post(
            "/api/v1/backups/ccm-20260909-030000.sqlite/restore"
        )
    assert response.status_code == 400
    assert database.read_bytes() == original
    paths = get_backup_paths(settings.database_url)
    assert not paths.marker_file.exists()
    assert not paths.pending_file.exists()


def test_incompatible_restore_candidate_leaves_production_untouched(backup_app):
    settings, client, database = backup_app
    configure(client)
    candidate = database.parent / "old-schema.db"
    sqlite_backup(database, candidate)
    with closing(sqlite3.connect(candidate)) as connection:
        connection.execute("UPDATE alembic_version SET version_num='99999999_9999_incompatible_future'")
        connection.commit()
    original = database.read_bytes()

    def runner(args, **_kwargs):
        if args[:2] == ["obscure", "-"]:
            return completed(args, "obscured")
        if "copyto" in args:
            Path(args[-1]).write_bytes(candidate.read_bytes())
        return completed(args)

    with patch("app.services.backup.run_rclone", side_effect=runner):
        response = client.post(
            "/api/v1/backups/ccm-20260909-030000.sqlite/restore"
        )
    assert response.status_code == 400
    assert database.read_bytes() == original
    paths = get_backup_paths(settings.database_url)
    assert not paths.marker_file.exists()
    assert not paths.pending_file.exists()


def test_offline_restore_no_pending_is_noop(backup_app):
    settings, client, database = backup_app
    client.app.state.engine.dispose()
    before = database.read_bytes()
    assert apply_restore_offline(settings.database_url) is False
    assert database.read_bytes() == before


def test_offline_restore_replaces_and_cleans_transaction_files(backup_app):
    settings, client, database = backup_app
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE restore_probe(value TEXT)")
        connection.execute("INSERT INTO restore_probe VALUES ('old')")
        connection.commit()
    paths = get_backup_paths(settings.database_url)
    sqlite_backup(database, paths.pending_file)
    with closing(sqlite3.connect(paths.pending_file)) as connection:
        connection.execute("UPDATE restore_probe SET value='new'")
        connection.commit()
    _write_marker(paths)
    client.app.state.engine.dispose()
    with patch("app.services.backup.sqlite_backup", wraps=sqlite_backup) as copier:
        assert apply_restore_offline(settings.database_url) is True
    assert any(call.args[1] == paths.rollback_file for call in copier.call_args_list)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT value FROM restore_probe").fetchone()[0] == "new"
    assert not paths.pending_file.exists()
    assert not paths.marker_file.exists()
    assert not paths.rollback_file.exists()


def test_offline_restore_failure_verifies_and_restores_rollback(backup_app):
    settings, client, database = backup_app
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE restore_probe(value TEXT)")
        connection.execute("INSERT INTO restore_probe VALUES ('old')")
        connection.commit()
    paths = get_backup_paths(settings.database_url)
    sqlite_backup(database, paths.pending_file)
    with closing(sqlite3.connect(paths.pending_file)) as connection:
        connection.execute("UPDATE restore_probe SET value='new'")
        connection.commit()
    _write_marker(paths)
    client.app.state.engine.dispose()

    real_verify = backup.verify_sqlite
    failed_once = False

    def fail_restored_database(path):
        nonlocal failed_once
        if Path(path) == database and paths.rollback_file.exists() and not failed_once:
            failed_once = True
            raise SystemExit("restored verification failed")
        return real_verify(path)

    with patch("app.services.backup.verify_sqlite", side_effect=fail_restored_database):
        with pytest.raises(SystemExit, match="已回滚"):
            apply_restore_offline(settings.database_url)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT value FROM restore_probe").fetchone()[0] == "old"
    assert not paths.pending_file.exists()
    assert not paths.marker_file.exists()
    assert not paths.rollback_file.exists()


def test_offline_restore_recovers_interrupted_post_swap_state(backup_app):
    settings, client, database = backup_app
    paths = get_backup_paths(settings.database_url)
    sqlite_backup(database, paths.rollback_file)
    _write_marker(paths)
    assert not paths.pending_file.exists()
    client.app.state.engine.dispose()
    assert apply_restore_offline(settings.database_url) is True
    assert not paths.marker_file.exists()
    assert not paths.rollback_file.exists()


def test_entrypoint_applies_pending_restore_before_migration_and_server_start():
    entrypoint = (
        Path(__file__).resolve().parents[1] / "scripts" / "docker-entrypoint.sh"
    ).read_text(encoding="utf-8")
    apply_at = entrypoint.index("apply-pending-restore-if-any")
    migrate_at = entrypoint.index("alembic upgrade head")
    verify_at = entrypoint.index("manage_db.py verify")
    uvicorn_at = entrypoint.index("exec uvicorn")
    assert apply_at < migrate_at < verify_at < uvicorn_at
    assert "--workers 1" in entrypoint
    assert "rm -rf -- /data/backups/local" in entrypoint


def test_update_settings_rejects_credentials_in_webdav_url(backup_app):
    _, client, _ = backup_app
    response = client.put(
        "/api/v1/backups/settings",
        json={"webdav_url": "https://user:pass@dav.example.test/root/"},
    )
    assert response.status_code == 422
    assert "独立凭据" in response.text


def test_cross_version_restore_n_minus_one_migrates_and_passes_strict_verify(backup_app):
    settings, client, database = backup_app
    paths = get_backup_paths(settings.database_url)

    # 1. Create a candidate database at historical Alembic revision 0001
    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{paths.pending_file.resolve().as_posix()}")
    with patch.dict(os.environ, {"CCM_DATABASE_URL": f"sqlite:///{paths.pending_file.resolve().as_posix()}"}):
        command.upgrade(cfg, "0001")

    # Insert test probe into candidate database
    with closing(sqlite3.connect(paths.pending_file)) as connection:
        connection.execute(
            "INSERT INTO cards (id, bank_code, bank_name, display_name, last4, currency, due_date_rule_type, due_date_rule_value, hidden, enabled, status, reminder_enabled, created_at, updated_at) "
            "VALUES ('probe-card-1', 'CMB', '招商银行', '跨版本测试卡', '9999', 'CNY', 'fixed_day', 25, 0, 1, 'active', 1, '2026-09-01T00:00:00', '2026-09-01T00:00:00')"
        )
        connection.commit()

    # Write restore marker
    _write_marker(paths)
    client.app.state.engine.dispose()

    # 2. Run offline restore with automatic migration
    assert apply_restore_offline(settings.database_url) is True

    # 3. Verify:
    # - Production DB now has the probe card
    # - alembic_version is at current head (0002)
    # - strict verify passes on production DB
    # - marker, pending, and rollback files are cleanly removed
    with closing(sqlite3.connect(database)) as connection:
        probe = connection.execute("SELECT display_name FROM cards WHERE id = 'probe-card-1'").fetchone()
        assert probe is not None and probe[0] == "跨版本测试卡"
        version_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version_row is not None and version_row[0] == "0002"

    backup.verify_sqlite(database)
    assert not paths.pending_file.exists()
    assert not paths.marker_file.exists()
    assert not paths.rollback_file.exists()


def test_cross_version_restore_migration_failure_rolls_back(backup_app):
    settings, client, database = backup_app
    paths = get_backup_paths(settings.database_url)

    # 1. Existing production DB has original probe
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE original_probe(value TEXT)")
        connection.execute("INSERT INTO original_probe VALUES ('original_production_data')")
        connection.commit()

    # 2. Create an older revision (0001) candidate DB
    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{paths.pending_file.resolve().as_posix()}")
    with patch.dict(os.environ, {"CCM_DATABASE_URL": f"sqlite:///{paths.pending_file.resolve().as_posix()}"}):
        command.upgrade(cfg, "0001")

    _write_marker(paths)
    client.app.state.engine.dispose()

    # 3. Mock alembic_upgrade inside apply_restore_offline to fail
    def fail_migration(cfg, rev):
        raise RuntimeError("Simulated migration script execution error")

    with patch("app.services.backup.alembic_upgrade", side_effect=fail_migration):
        with pytest.raises(SystemExit, match="恢复后的数据库升级或验证失败，已回滚原数据库"):
            apply_restore_offline(settings.database_url)

    # 4. Verify original production DB is restored intact
    with closing(sqlite3.connect(database)) as connection:
        probe = connection.execute("SELECT value FROM original_probe").fetchone()
        assert probe is not None and probe[0] == "original_production_data"

    backup.verify_sqlite(database)
    assert not paths.pending_file.exists()
    assert not paths.marker_file.exists()
    assert not paths.rollback_file.exists()
