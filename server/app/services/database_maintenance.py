from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from app import models  # noqa: F401
from app.db.base import Base


def database_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise SystemExit("数据库维护脚本当前只支持文件型 SQLite 数据库")
    return Path(url.database).expanduser().resolve()


def verify_candidate_sqlite(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"数据库不存在：{path}")
    try:
        with closing(sqlite3.connect(path)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise SystemExit(f"SQLite 完整性检查失败：{result[0] if result else '无结果'}")
            migrations = {
                row[0]
                for row in connection.execute("SELECT version_num FROM alembic_version")
            }
            if not migrations:
                raise SystemExit("数据库缺少 Alembic 迁移版本")

            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            # Verify that core CCM tables exist in candidate
            core_tables = {"cards", "statements", "repayments", "alembic_version"}
            if not core_tables <= tables:
                missing = core_tables - tables
                raise SystemExit(f"数据库缺少核心 CCM 表：{','.join(sorted(missing))}")
    except sqlite3.Error as exc:
        raise SystemExit(f"SQLite 完整性检查失败：{exc}")

    config_path = Path(__file__).resolve().parents[2] / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(config_path)))
    valid_revisions = {rev.revision for rev in script.walk_revisions()}
    if not migrations <= valid_revisions:
        invalid = migrations - valid_revisions
        raise SystemExit(f"备份包含未知的未来或非法迁移版本：{','.join(sorted(invalid))}")
    print(
        "预检通过：SQLite integrity_check=ok，migration="
        + ",".join(sorted(migrations))
    )


def verify_sqlite(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"数据库不存在：{path}")
    try:
        with closing(sqlite3.connect(path)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise SystemExit(f"SQLite 完整性检查失败：{result[0] if result else '无结果'}")
            migrations = {
                row[0]
                for row in connection.execute("SELECT version_num FROM alembic_version")
            }
            if not migrations:
                raise SystemExit("数据库缺少 Alembic 迁移版本")

            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            for table in Base.metadata.sorted_tables:
                if table.name not in tables:
                    raise SystemExit(f"数据库缺少必要 CCM 表：{table.name}")
                columns = {
                    row[1]
                    for row in connection.execute(
                        f'PRAGMA table_info("{table.name}")'
                    )
                }
                missing = set(table.columns.keys()) - columns
                if missing:
                    raise SystemExit(
                        f"CCM 表 {table.name} 缺少必要列：{','.join(sorted(missing))}"
                    )
                actual_foreign_keys = {
                    (row[3], row[2], row[4], row[6].upper())
                    for row in connection.execute(
                        f'PRAGMA foreign_key_list("{table.name}")'
                    )
                }
                required_foreign_keys = {
                    (
                        foreign_key.parent.name,
                        foreign_key.column.table.name,
                        foreign_key.column.name,
                        (foreign_key.ondelete or "NO ACTION").upper(),
                    )
                    for foreign_key in table.foreign_keys
                }
                if not required_foreign_keys <= actual_foreign_keys:
                    raise SystemExit(f"CCM 表 {table.name} 外键 contract 不完整")
    except sqlite3.Error as exc:
        raise SystemExit(f"SQLite 完整性检查失败：{exc}")
    config_path = Path(__file__).resolve().parents[2] / "alembic.ini"
    expected_heads = set(
        ScriptDirectory.from_config(Config(str(config_path))).get_heads()
    )
    if migrations != expected_heads:
        raise SystemExit(
            "数据库迁移不是最新 head："
            f"current={','.join(sorted(migrations))}，"
            f"expected={','.join(sorted(expected_heads))}"
        )
    print(
        "验证通过：SQLite integrity_check=ok，migration="
        + ",".join(sorted(migrations))
    )


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_db, closing(
        sqlite3.connect(destination)
    ) as target_db:
        source_db.backup(target_db)
    verify_sqlite(destination)
