from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Iterator
import uuid
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.plain_secrets import get_secret
from app.core.redaction import redact_sensitive
from app.models.backup import BackupSetting
from alembic.config import Config
from alembic.command import upgrade as alembic_upgrade
from app.services.database_maintenance import sqlite_backup, verify_candidate_sqlite, verify_sqlite

logger = logging.getLogger(__name__)

BACKUP_FILENAME_PATTERN = re.compile(r"^ccm-\d{8}-\d{6}\.sqlite$")
BACKUP_OPERATION_LOCK = threading.Lock()
PLAIN_REMOTE_DIR = "ccm-backups-plain"


class BackupBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupPaths:
    database_file: Path
    staging_dir: Path
    restore_dir: Path

    @property
    def pending_file(self) -> Path:
        return self.restore_dir / "pending.sqlite"

    @property
    def marker_file(self) -> Path:
        return self.restore_dir / "pending.json"

    @property
    def rollback_file(self) -> Path:
        return self.restore_dir / "rollback.sqlite"


def get_backup_paths(database_url: str) -> BackupPaths:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("备份服务仅支持文件型 SQLite 数据库")
    database_file = Path(url.database).expanduser().resolve()
    backup_dir = database_file.parent / "backups"
    staging_dir = backup_dir / "staging"
    restore_dir = backup_dir / "restore"
    staging_dir.mkdir(parents=True, exist_ok=True)
    restore_dir.mkdir(parents=True, exist_ok=True)
    _chmod(restore_dir, 0o700)
    for stale in staging_dir.glob("ccm-????????-??????.sqlite"):
        stale.unlink(missing_ok=True)
    return BackupPaths(database_file, staging_dir, restore_dir)


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        logger.warning("Could not restrict permissions for %s", path.name)


@contextmanager
def backup_operation() -> Iterator[None]:
    # ponytail: process lock matches the supported single-worker deployment; use a
    # distributed lock only if the deployment model changes.
    if not BACKUP_OPERATION_LOCK.acquire(blocking=False):
        raise BackupBusyError("已有备份、清理或恢复操作正在执行")
    try:
        yield
    finally:
        BACKUP_OPERATION_LOCK.release()


def find_rclone_binary() -> str:
    path = shutil.which("rclone")
    if path:
        return path
    exe_dir = Path(sys.executable).parent
    for name in ("rclone", "rclone.exe"):
        candidate = exe_dir / name
        if candidate.is_file():
            return str(candidate)
    return "rclone"


def _format_rclone_error(stderr: str, returncode: int) -> str:
    raw = stderr.strip() or f"rclone exited with code {returncode}"
    lowered = raw.lower()
    if "409" in lowered or "conflict" in lowered or "updatemkparentdir" in lowered or "directory not found" in lowered or "not found" in lowered:
        return "WebDAV 路径不存在或未挂载"
    if "unauthorized" in lowered or "401" in lowered:
        return "WebDAV 认证失败：用户名或密码错误"
    if "connection refused" in lowered or "timeout" in lowered or "dial tcp" in lowered:
        return "WebDAV 连接失败：网络超时或地址无法访问"
    for line in raw.splitlines():
        line = line.strip()
        if "ERROR :" in line:
            _, _, msg = line.partition("ERROR :")
            return msg.strip()
    return raw.splitlines()[-1].strip() if raw.splitlines() else raw


def run_rclone(
    args: list[str],
    env: dict[str, str] | None = None,
    stdin_input: bytes | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    cmd = [find_rclone_binary(), *args]
    full_env = {**os.environ, **(env or {})}
    logger.info("Executing rclone %s", " ".join(args[:3]))
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_input.decode("utf-8") if stdin_input is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=full_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"rclone timed out after {timeout} seconds") from exc
    if proc.returncode != 0:
        raise RuntimeError(_format_rclone_error(proc.stderr, proc.returncode))
    return proc


def obscure_password(plaintext: str) -> str:
    return run_rclone(
        ["obscure", "-"],
        stdin_input=plaintext.encode("utf-8") + b"\n",
    ).stdout.strip()


def build_rclone_env(
    backup_password: str | None,
    webdav_url: str,
    webdav_username: str | None = None,
    webdav_password: str | None = None,
    webdav_remote_path: str = "ccm-backups",
) -> dict[str, str]:
    url = webdav_url.strip()
    if not url:
        raise ValueError("未配置 WebDAV，无法执行备份")
    if not url.endswith("/"):
        url += "/"
    subfolder = webdav_remote_path.strip("/")
    env = {
        "RCLONE_CONFIG_CCM_WEBDAV_TYPE": "webdav",
        "RCLONE_CONFIG_CCM_WEBDAV_URL": url,
        "RCLONE_CONFIG_CCM_WEBDAV_VENDOR": "other",
        "RCLONE_CONFIG_CCM_WEBDAV_USER": webdav_username or "",
        "RCLONE_HEADER": "Referer:",
    }
    if webdav_password:
        env["RCLONE_CONFIG_CCM_WEBDAV_PASS"] = obscure_password(webdav_password)
    if backup_password:
        env["RCLONE_CONFIG_CCM_WEBDAV_CRYPT_TYPE"] = "crypt"
        env["RCLONE_CONFIG_CCM_WEBDAV_CRYPT_REMOTE"] = f"CCM_WEBDAV:{subfolder}" if subfolder else "CCM_WEBDAV:"
        env["RCLONE_CONFIG_CCM_WEBDAV_CRYPT_PASSWORD"] = obscure_password(backup_password)
    return env


def _remote_target(env: dict[str, str]) -> str:
    if env.get("RCLONE_CONFIG_CCM_WEBDAV_CRYPT_TYPE"):
        return "CCM_WEBDAV_CRYPT:"
    return f"CCM_WEBDAV:{PLAIN_REMOTE_DIR}/"


def _ensure_remote_dir(env: dict[str, str]) -> None:
    run_rclone(["--config", "", "mkdir", _remote_target(env).rstrip("/")], env=env)


def get_backup_setting(session: Session, *, create: bool = True) -> BackupSetting | None:
    setting = session.scalar(select(BackupSetting).where(BackupSetting.id == "default"))
    if setting is None and create:
        setting = BackupSetting(id="default")
        session.add(setting)
        session.commit()
        session.refresh(setting)
    return setting


def _remote_env(
    setting: BackupSetting,
    *,
    encrypted: bool | None = None,
    crypt_password: str | None = None,
) -> dict[str, str]:
    if not setting.webdav_url or not setting.webdav_url.strip():
        raise ValueError("未配置 WebDAV，无法执行备份")
    allow_unencrypted = bool(getattr(setting, "allow_unencrypted_backup", False))
    if encrypted is not None and encrypted == allow_unencrypted:
        raise ValueError("备份加密模式与当前设置不一致")
    password: str | None = None
    if not allow_unencrypted:
        password = crypt_password or get_secret("backup", "crypt_password")
        if not password:
            raise ValueError("未配置备份密码，无法执行备份。如需明文备份，请显式开启允许明文备份。")
    return build_rclone_env(
        password,
        setting.webdav_url,
        setting.webdav_username,
        get_secret("backup", "webdav_password"),
    )


def _backup_time(name: str, timezone: str) -> datetime:
    return datetime.strptime(name, "ccm-%Y%m%d-%H%M%S.sqlite").replace(
        tzinfo=ZoneInfo(timezone)
    )


def list_remote_backups(env: dict[str, str], timezone: str) -> list[dict[str, Any]]:
    try:
        output = run_rclone(
            ["--config", "", "lsjson", "--files-only", _remote_target(env)], env=env
        ).stdout
    except RuntimeError as exc:
        message = str(exc).lower()
        if "directory not found" in message or "not found" in message:
            return []
        raise
    try:
        items = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("rclone 返回了无效的备份列表") from exc
    if not isinstance(items, list):
        raise RuntimeError("rclone 返回了无效的备份列表")
    backups = []
    for item in items:
        if not isinstance(item, dict) or item.get("IsDir"):
            continue
        name = item.get("Path") or item.get("Name")
        if not isinstance(name, str) or not BACKUP_FILENAME_PATTERN.fullmatch(name):
            continue
        try:
            backup_time = _backup_time(name, timezone)
        except ValueError:
            continue
        backups.append(
            {
                "name": name,
                "backup_time": backup_time.isoformat(),
                "size": max(0, int(item.get("Size", 0))),
                "mod_time": item.get("ModTime") if isinstance(item.get("ModTime"), str) else None,
            }
        )
    return sorted(backups, key=lambda item: item["backup_time"], reverse=True)


def list_backups(session: Session, settings: Settings) -> list[dict[str, Any]]:
    setting = get_backup_setting(session, create=False)
    if setting is None:
        raise ValueError("未配置 WebDAV")
    if not getattr(setting, "allow_unencrypted_backup", False) and not get_secret("backup", "crypt_password"):
        return []
    return list_remote_backups(_remote_env(setting), settings.timezone)


def _cleanup_remote(
    env: dict[str, str], timezone: str, retention_count: int
) -> dict[str, int]:
    backups = list_remote_backups(env, timezone)
    if len(backups) <= retention_count:
        return {"kept": len(backups), "deleted": 0, "failed": 0}
    kept = min(len(backups), retention_count)
    deleted = failed = 0
    for backup in backups[retention_count:]:
        try:
            run_rclone(
                ["--config", "", "deletefile", f"{_remote_target(env)}{backup['name']}"],
                env=env,
            )
            deleted += 1
        except Exception as exc:
            failed += 1
            logger.warning("Failed to delete old CCM backup %s (%s)", backup["name"], type(exc).__name__)
    return {"kept": kept, "deleted": deleted, "failed": failed}


def cleanup_backups(session: Session, settings: Settings) -> dict[str, int]:
    with backup_operation():
        paths = get_backup_paths(settings.database_url)
        if paths.marker_file.exists():
            raise BackupBusyError("数据库恢复正在等待应用")
        setting = get_backup_setting(session, create=False)
        if setting is None:
            raise ValueError("未配置 WebDAV")
        return _cleanup_remote(
            _remote_env(setting), settings.timezone, setting.retention_count
        )


def delete_backup(session: Session, settings: Settings, backup_name: str) -> None:
    validate_backup_name(backup_name)
    with backup_operation():
        paths = get_backup_paths(settings.database_url)
        if paths.marker_file.exists():
            raise BackupBusyError("数据库恢复正在等待应用")
        setting = get_backup_setting(session, create=False)
        if setting is None:
            raise ValueError("未配置 WebDAV")
        env = _remote_env(setting)
        run_rclone(
            ["--config", "", "deletefile", f"{_remote_target(env)}{backup_name}"],
            env=env,
        )


def execute_backup(
    session: Session,
    settings: Settings,
    *,
    encrypted: bool | None = None,
    backup_password: str | None = None,
) -> dict[str, Any]:
    with backup_operation():
        setting = get_backup_setting(session)
        assert setting is not None
        paths = get_backup_paths(settings.database_url)
        if paths.marker_file.exists():
            raise BackupBusyError("数据库恢复正在等待应用")
        now = datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None)
        setting.last_attempt_at = now
        staging_file: Path | None = None
        try:
            env = _remote_env(
                setting, encrypted=encrypted, crypt_password=backup_password
            )
            filename = f"ccm-{now.strftime('%Y%m%d-%H%M%S')}.sqlite"
            staging_file = paths.staging_dir / filename
            sqlite_backup(paths.database_file, staging_file)
            _ensure_remote_dir(env)
            run_rclone(
                ["--config", "", "copyto", str(staging_file), f"{_remote_target(env)}{filename}"],
                env=env,
            )
            staging_file.unlink()
            staging_file = None
            setting.last_remote_backup_at = now
            setting.last_remote_status = "SUCCESS"
            setting.last_error = None
            warning = None
            try:
                cleanup = _cleanup_remote(env, settings.timezone, setting.retention_count)
                if cleanup["failed"]:
                    warning = f"备份成功，但有 {cleanup['failed']} 个旧备份清理失败"
            except Exception as exc:
                warning = "备份成功，但保留规则清理失败"
                logger.warning("Backup retention cleanup failed (%s)", type(exc).__name__)
            setting.last_error = warning
            session.commit()
            return {"filename": filename, "status": "SUCCESS", "warning": warning}
        except Exception as exc:
            setting.last_remote_status = "FAILED"
            setting.last_error = redact_sensitive(exc)
            session.commit()
            raise
        finally:
            if staging_file is not None:
                staging_file.unlink(missing_ok=True)


def test_webdav_connection(session: Session, settings: Settings) -> dict[str, Any]:
    setting = get_backup_setting(session, create=False)
    if setting is None:
        raise ValueError("未配置 WebDAV")
    env = _remote_env(setting)
    probe_name = f"probe-{uuid.uuid4().hex}.tmp"
    content = os.urandom(64)
    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "probe.bin"
        target = Path(td) / "probe-downloaded.bin"
        source.write_bytes(content)
        _ensure_remote_dir(env)
        try:
            try:
                run_rclone(
                    ["--config", "", "copyto", str(source), f"{_remote_target(env)}{probe_name}"],
                    env=env,
                )
                run_rclone(
                    ["--config", "", "copyto", f"{_remote_target(env)}{probe_name}", str(target)],
                    env=env,
                )
                if not target.is_file() or target.read_bytes() != content:
                    raise RuntimeError("下载验证失败：文件内容不一致")
            except RuntimeError as exc:
                message = str(exc).lower()
                if "404" in message or "not found" in message or "directory not found" in message or "409" in message or "conflict" in message:
                    raise ValueError(
                        "WebDAV 路径不存在或未挂载"
                    ) from exc
                raise
        finally:
            try:
                run_rclone(
                    ["--config", "", "deletefile", f"{_remote_target(env)}{probe_name}"], env=env
                )
            except Exception as exc:
                logger.warning("Failed to delete WebDAV probe (%s)", type(exc).__name__)
    setting.webdav_verified_at = datetime.now(ZoneInfo(settings.timezone)).replace(
        tzinfo=None
    )
    session.commit()
    mode = "加密" if env.get("RCLONE_CONFIG_CCM_WEBDAV_CRYPT_TYPE") else "明文"
    return {"success": True, "message": f"WebDAV 连接与{mode}读写测试通过"}


def backup_due(setting: BackupSetting | None, now: datetime, timezone: str) -> bool:
    if setting is None or setting.schedule_type != "cron":
        return False
    previous = croniter(setting.cron_expr, now + timedelta(seconds=1)).get_prev(datetime)
    last_attempt = setting.last_attempt_at
    if last_attempt is not None and last_attempt.tzinfo is None:
        last_attempt = last_attempt.replace(tzinfo=ZoneInfo(timezone))
    return last_attempt is None or last_attempt < previous


def next_backup_run(setting: BackupSetting, timezone: str) -> str | None:
    if setting.schedule_type != "cron":
        return None
    now = datetime.now(ZoneInfo(timezone))
    return croniter(setting.cron_expr, now).get_next(datetime).isoformat()


def validate_backup_name(backup_name: str) -> str:
    if not BACKUP_FILENAME_PATTERN.fullmatch(backup_name):
        raise ValueError("备份文件名格式无效")
    return backup_name


def download_restore_candidate(
    backup_name: str, candidate: Path, env: dict[str, str]
) -> None:
    validate_backup_name(backup_name)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.touch(mode=0o600, exist_ok=True)
    _chmod(candidate, 0o600)
    run_rclone(
        ["--config", "", "copyto", f"{_remote_target(env)}{backup_name}", str(candidate)],
        env=env,
    )
    _chmod(candidate, 0o600)


def validate_restore_candidate(candidate: Path) -> None:
    try:
        verify_candidate_sqlite(candidate)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc


def _prepare_pending(
    paths: BackupPaths,
    backup_name: str,
    requested_at: str,
    env: dict[str, str],
) -> None:
    if paths.marker_file.exists():
        raise BackupBusyError("已有数据库恢复正在等待应用")
    paths.pending_file.unlink(missing_ok=True)
    paths.rollback_file.unlink(missing_ok=True)
    marker = {"backup_name": backup_name, "requested_at": requested_at}
    paths.marker_file.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
    _chmod(paths.marker_file, 0o600)
    try:
        download_restore_candidate(backup_name, paths.pending_file, env)
        validate_restore_candidate(paths.pending_file)
    except (Exception, SystemExit):
        paths.pending_file.unlink(missing_ok=True)
        paths.marker_file.unlink(missing_ok=True)
        raise


def prepare_pending_restore(
    session: Session, settings: Settings, backup_name: str
) -> dict[str, str]:
    validate_backup_name(backup_name)
    with backup_operation():
        setting = get_backup_setting(session, create=False)
        if setting is None:
            raise ValueError("未配置 WebDAV")
        paths = get_backup_paths(settings.database_url)
        _prepare_pending(
            paths,
            backup_name,
            datetime.now(ZoneInfo(settings.timezone)).isoformat(),
            _remote_env(setting),
        )
        return {"status": "PENDING_RESTART", "backup_name": backup_name}


def apply_restore_offline(database_url: str) -> bool:
    paths = get_backup_paths(database_url)
    if not paths.marker_file.exists():
        return False
    try:
        marker = json.loads(paths.marker_file.read_text(encoding="utf-8"))
        validate_backup_name(marker["backup_name"])
        if paths.pending_file.exists():
            validate_restore_candidate(paths.pending_file)
            if paths.database_file.exists() and not paths.rollback_file.exists():
                sqlite_backup(paths.database_file, paths.rollback_file)
                _chmod(paths.rollback_file, 0o600)
            os.replace(paths.pending_file, paths.database_file)
        elif not paths.rollback_file.exists():
            verify_sqlite(paths.database_file)
            paths.marker_file.unlink(missing_ok=True)
            return True

        # Preflight verification on the placed restored database
        validate_restore_candidate(paths.database_file)

        # Run alembic upgrade head to migrate old-version backup schemas forward
        config_path = Path(__file__).resolve().parents[2] / "alembic.ini"
        alembic_cfg = Config(str(config_path))
        target_db_url = f"sqlite:///{paths.database_file.resolve().as_posix()}"
        alembic_cfg.set_main_option("sqlalchemy.url", target_db_url)
        old_env_url = os.environ.get("CCM_DATABASE_URL")
        try:
            try:
                os.environ["CCM_DATABASE_URL"] = target_db_url
                alembic_upgrade(alembic_cfg, "head")
                verify_sqlite(paths.database_file)
            finally:
                if old_env_url is not None:
                    os.environ["CCM_DATABASE_URL"] = old_env_url
                else:
                    os.environ.pop("CCM_DATABASE_URL", None)
        except (Exception, SystemExit):
            if not paths.rollback_file.exists():
                raise
            os.replace(paths.rollback_file, paths.database_file)
            verify_sqlite(paths.database_file)
            raise SystemExit("恢复后的数据库升级或验证失败，已回滚原数据库")

        paths.rollback_file.unlink(missing_ok=True)
        paths.marker_file.unlink(missing_ok=True)
        return True
    except (Exception, SystemExit):
        if paths.rollback_file.exists():
            os.replace(paths.rollback_file, paths.database_file)
            try:
                verify_sqlite(paths.database_file)
            except Exception:
                pass
        paths.pending_file.unlink(missing_ok=True)
        paths.marker_file.unlink(missing_ok=True)
        paths.rollback_file.unlink(missing_ok=True)
        raise


def prepare_pending_restore_with_credentials(
    database_url: str,
    backup_name: str,
    backup_password: str | None,
    webdav_url: str,
    webdav_username: str | None = None,
    webdav_password: str | None = None,
) -> None:
    validate_backup_name(backup_name)
    with backup_operation():
        paths = get_backup_paths(database_url)
        _prepare_pending(
            paths,
            backup_name,
            datetime.now().astimezone().isoformat(),
            build_rclone_env(
                backup_password, webdav_url, webdav_username, webdav_password
            ),
        )
