from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.database_maintenance import (
    database_path,
    verify_sqlite,
)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="SQLite 数据库验证与 WebDAV 备份恢复")
    parser.add_argument(
        "--database-url",
        default=os.getenv("CCM_DATABASE_URL", "sqlite:///./data/credit-card-manager.db"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify")
    rclone_backup_parser = subparsers.add_parser("rclone-backup", help="执行一次 WebDAV 备份（可选加密）")
    rclone_backup_parser.add_argument("--no-encryption", action="store_true", help="强制明文备份，忽略已保存的备份密码")
    rclone_backup_parser.add_argument("--encrypted", action="store_true", help="强制加密备份（保险库/环境变量/交互提示）")
    rclone_restore_parser = subparsers.add_parser("rclone-restore", help="从 WebDAV 备份恢复数据库（可选加密）")
    rclone_restore_parser.add_argument("backup_name", help="备份文件逻辑名称 (如 ccm-20260909-120000.sqlite)")
    rclone_restore_parser.add_argument("--yes", action="store_true", help="确认服务已停止并替换数据库")
    rclone_restore_parser.add_argument("--no-encryption", action="store_true", help="强制从明文备份目录恢复")
    rclone_restore_parser.add_argument("--encrypted", action="store_true", help="强制从加密备份恢复（保险库/环境变量/交互提示）")
    subparsers.add_parser("apply-pending-restore-if-any")
    args = parser.parse_args()

    if getattr(args, "encrypted", False) and getattr(args, "no_encryption", False):
        raise SystemExit("--encrypted 与 --no-encryption 不能同时使用")

    target = database_path(args.database_url)
    if args.command == "verify":
        verify_sqlite(target)
        return

    if args.command == "rclone-backup":
        from app.core.config import Settings
        from app.db.session import create_database_engine, create_session_factory
        from app.services.backup import execute_backup

        encrypted: bool | None = None
        if args.no_encryption:
            encrypted = False
        elif args.encrypted:
            encrypted = True
        cli_password: str | None = None
        if encrypted:
            import getpass

            from app.core.plain_secrets import get_secret

            cli_password = get_secret("backup", "crypt_password") or os.getenv(
                "CCM_BACKUP_PASSWORD"
            )
            if not cli_password:
                cli_password = (
                    getpass.getpass("请输入备份密码 (Backup Password): ").strip() or None
                )
            if not cli_password:
                raise SystemExit("未提供备份密码，无法加密备份")

        settings = Settings(database_url=args.database_url)
        engine = create_database_engine(settings.database_url)
        session_factory = create_session_factory(engine, settings.timezone)
        try:
            with session_factory() as session:
                result = execute_backup(
                    session,
                    settings,
                    encrypted=encrypted,
                    backup_password=cli_password,
                )
        finally:
            engine.dispose()
        print(
            "WebDAV 备份完成："
            f"file={result['filename']}，status={result['status']}"
        )
        if result.get("warning"):
            print(f"警告：{result['warning']}")
        return

    if args.command == "apply-pending-restore-if-any":
        from app.services.backup import apply_restore_offline

        if apply_restore_offline(args.database_url):
            print("已应用待处理的数据库恢复")
        return

    if args.command == "rclone-restore":
        from contextlib import closing
        import getpass
        import sqlite3
        from app.core.plain_secrets import get_secret
        from app.services.backup import (
            apply_restore_offline,
            prepare_pending_restore_with_credentials,
        )

        if not args.yes:
            raise SystemExit("恢复会替换当前数据库；确认服务已停止后追加 --yes")

        webdav_url: str | None = None
        webdav_user: str | None = None

        if target.is_file():
            try:
                with closing(sqlite3.connect(target)) as conn:
                    row = conn.execute(
                        "SELECT webdav_url, webdav_username FROM backup_settings WHERE id = 'default'"
                    ).fetchone()
                    if row:
                        webdav_url, webdav_user = row
            except Exception:
                pass

        resolved_pass: str | None = None
        if not args.no_encryption:
            resolved_pass = get_secret("backup", "crypt_password") or os.getenv(
                "CCM_BACKUP_PASSWORD"
            )
            if not resolved_pass and args.encrypted:
                resolved_pass = (
                    getpass.getpass("请输入备份密码 (Backup Password): ").strip() or None
                )
            if args.encrypted and not resolved_pass:
                raise SystemExit("未提供备份密码，无法恢复")

        webdav_pass = get_secret("backup", "webdav_password")
        if not webdav_url:
            print("当前数据库未记录有效的 WebDAV 配置，请输入连接信息：")
            webdav_url = input("WebDAV 地址: ").strip()
            webdav_user = input("WebDAV 用户名: ").strip()
            webdav_pass = getpass.getpass("WebDAV 密码: ").strip() or None
        elif not webdav_pass:
            webdav_pass = getpass.getpass("WebDAV 密码（无密码直接回车）: ").strip() or None
        if not webdav_url:
            raise SystemExit("未提供 WebDAV 地址，无法恢复")

        prepare_pending_restore_with_credentials(
            args.database_url,
            args.backup_name,
            resolved_pass,
            webdav_url=webdav_url,
            webdav_username=webdav_user,
            webdav_password=webdav_pass,
        )
        apply_restore_offline(args.database_url)
        print(f"WebDAV 恢复完成：{target}")
        return

if __name__ == "__main__":
    main()
