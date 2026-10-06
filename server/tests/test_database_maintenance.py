import os
from pathlib import Path
import sqlite3
from unittest.mock import patch

from alembic import command
from alembic.config import Config
import pytest

from app.services.database_maintenance import (
    sqlite_backup,
    verify_candidate_sqlite,
    verify_sqlite,
)


def _create_migrated_database(path: Path, value: str) -> None:
    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    with patch.dict(os.environ, {"CCM_DATABASE_URL": f"sqlite:///{path.as_posix()}"}):
        command.upgrade(cfg, "head")
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def test_sqlite_backup_is_valid_and_releases_windows_file_handles(tmp_path: Path) -> None:
    database = tmp_path / "database.db"
    backup = tmp_path / "backup.db"
    restored = tmp_path / "restored.db"
    _create_migrated_database(database, "original")

    sqlite_backup(database, backup)
    sqlite_backup(backup, restored)
    # This is the operation used by restore. It fails on Windows if either
    # sqlite3 connection from backup/verification was not explicitly closed.
    os.replace(restored, database)
    verify_sqlite(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT value FROM sample").fetchone() == ("original",)
    finally:
        connection.close()


def test_verify_sqlite_rejects_incomplete_ccm_schema(tmp_path: Path) -> None:
    database = tmp_path / "incomplete.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.execute(
            "INSERT INTO alembic_version VALUES (?)", ("0002",)
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SystemExit, match="缺少必要 CCM 表"):
        verify_sqlite(database)


def test_verify_sqlite_rejects_outdated_revision(tmp_path: Path) -> None:
    database = tmp_path / "outdated.db"
    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    with patch.dict(os.environ, {"CCM_DATABASE_URL": f"sqlite:///{database.as_posix()}"}):
        command.upgrade(cfg, "0001")

    with pytest.raises(SystemExit):
        verify_sqlite(database)


def test_verify_candidate_sqlite_accepts_valid_prior_revision(tmp_path: Path) -> None:
    database = tmp_path / "prior_rev.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.execute("INSERT INTO alembic_version VALUES (?)", ("0001",))
        connection.execute("CREATE TABLE cards (id VARCHAR(36) PRIMARY KEY)")
        connection.execute("CREATE TABLE statements (id VARCHAR(36) PRIMARY KEY)")
        connection.execute("CREATE TABLE repayments (id VARCHAR(36) PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    # Preflight must pass for valid historical revision 0001 in current Alembic revision tree
    verify_candidate_sqlite(database)


def test_verify_candidate_sqlite_accepts_current_head_revision(tmp_path: Path) -> None:
    database = tmp_path / "head_rev.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.execute("INSERT INTO alembic_version VALUES (?)", ("0002",))
        connection.execute("CREATE TABLE cards (id VARCHAR(36) PRIMARY KEY)")
        connection.execute("CREATE TABLE statements (id VARCHAR(36) PRIMARY KEY)")
        connection.execute("CREATE TABLE repayments (id VARCHAR(36) PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    # Preflight must pass for current head revision 0002
    verify_candidate_sqlite(database)


def test_verify_candidate_sqlite_rejects_unknown_future_revision(tmp_path: Path) -> None:
    database = tmp_path / "unknown_rev.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.execute("INSERT INTO alembic_version VALUES (?)", ("9999_future_rev",))
        connection.execute("CREATE TABLE cards (id VARCHAR(36) PRIMARY KEY)")
        connection.execute("CREATE TABLE statements (id VARCHAR(36) PRIMARY KEY)")
        connection.execute("CREATE TABLE repayments (id VARCHAR(36) PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SystemExit, match="未知的未来或非法迁移版本"):
        verify_candidate_sqlite(database)


def test_verify_candidate_sqlite_rejects_missing_core_tables(tmp_path: Path) -> None:
    database = tmp_path / "missing_cards.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.execute("INSERT INTO alembic_version VALUES (?)", ("0001",))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SystemExit, match="缺少核心 CCM 表"):
        verify_candidate_sqlite(database)
