# ruff: noqa: RUF002
"""管理员整库备份与恢复的本机维护命令行。

只面向服务器或开发电脑本机执行，不提供网页恢复接口。子命令：

- ``backup create``：生成自动／手工／维护前备份；
- ``backup verify``：离线校验包结构、manifest、SHA-256 与压缩 SQL；
- ``backup prune``：按日／周／月分层预览或执行清理，默认 dry-run；
- ``restore prepare``：把备份恢复到新的空数据库；
- ``restore verify``：核对 alembic_version、表清单、各表数量及当前 ORM 结构并输出冒烟清单；
- ``rehearsal``：在严格限定名称的隔离测试库完成备份→恢复→核对→清理演练。

安全边界：目标库名需要双重确认；永久拒绝 ``gift_ledger_v2``、``contact_db`` 和非允许前缀；
数据库密码只进入临时凭据文件；恢复命令绝不改 ``.env`` 或切换正式连接。
"""

from __future__ import annotations

import argparse
import secrets
import sys
from collections.abc import Callable, Sequence

from app.core.config import Settings
from app.services.admin_backup import AdminBackupManager, BackupUnavailableError
from app.services.backup_index import (
    OFFSITE_FAILED,
    OFFSITE_NOT_CONFIGURED,
    BackupIndexCorruptedError,
    package_path_for,
)
from app.services.database_backup import (
    BACKUP_TYPE_AUTOMATIC,
    BACKUP_TYPE_MANUAL,
    BACKUP_TYPE_PRE_MAINTENANCE,
    BACKUP_TYPE_REHEARSAL,
    DatabaseBackupError,
    parse_admin_backup,
    parse_mysql_connection,
)
from app.services.database_restore import (
    ALLOWED_TEST_PREFIX,
    RestoreError,
    drop_test_database,
    restore_prepare,
    verify_restored_database,
    verify_restored_schema_head,
)
from app.services.restored_application_smoke import run_restored_application_smoke
from app.services.task_lock import TaskLockBusyError

EXIT_SUCCESS = 0
EXIT_SYSTEM_ERROR = 1
EXIT_INPUT_ERROR = 2

InputFunction = Callable[[str], str]


def _report_offsite_result(record, output: Callable[[str], None]) -> int:
    output(f"异地副本状态：{record.offsite_status}")
    if record.offsite_status == OFFSITE_FAILED:
        output("本地备份已保留，但异地加密副本失败，请立即检查挂载目录和 age 配置。")
        return EXIT_SYSTEM_ERROR
    if record.offsite_status == OFFSITE_NOT_CONFIGURED:
        output("提示：尚未配置加密异地副本，正式部署前必须完成配置。")
    return EXIT_SUCCESS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="往来有礼整库备份与本机恢复")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="整库备份命令")
    backup_sub = backup_parser.add_subparsers(dest="backup_command", required=True)

    create_parser = backup_sub.add_parser("create", help="生成整库备份")
    create_parser.add_argument(
        "--type",
        choices=[BACKUP_TYPE_AUTOMATIC, BACKUP_TYPE_MANUAL, BACKUP_TYPE_PRE_MAINTENANCE],
        default=BACKUP_TYPE_MANUAL,
        help="备份类型",
    )

    verify_parser = backup_sub.add_parser("verify", help="离线校验备份包")
    verify_parser.add_argument("backup_id", help="备份编号")

    prune_parser = backup_sub.add_parser("prune", help="按分层规则清理自动备份")
    prune_parser.add_argument("--execute", action="store_true", help="实际执行清理；默认只预览")

    restore_parser = subparsers.add_parser("restore", help="整库恢复命令")
    restore_sub = restore_parser.add_subparsers(dest="restore_command", required=True)

    prepare_parser = restore_sub.add_parser("prepare", help="恢复到新的空数据库")
    prepare_parser.add_argument("--backup", required=True, help="备份编号")
    prepare_parser.add_argument("--target", required=True, help="目标库名")
    prepare_parser.add_argument("--confirm-target", help="再次输入的目标库名，必须完全一致")
    prepare_parser.add_argument(
        "--allow-test-prefix",
        action="store_true",
        help="仅演练时允许使用隔离测试库前缀",
    )

    restore_verify_parser = restore_sub.add_parser("verify", help="核对恢复库")
    restore_verify_parser.add_argument("--backup", required=True, help="备份编号")
    restore_verify_parser.add_argument("--target", required=True, help="已恢复的目标库名")
    restore_verify_parser.add_argument(
        "--confirm-smoke-complete",
        action="store_true",
        help="确认已人工完成登录、查账、统计、记账和用户隔离冒烟，并把备份标为已验证",
    )

    subparsers.add_parser("rehearsal", help="在隔离测试库完成备份→恢复→核对→清理演练")

    return parser


def _settings() -> Settings:
    return Settings()


def _connection(settings: Settings):
    if settings.database_url is None:
        raise DatabaseBackupError("backup_connection_unavailable", "尚未配置数据库地址")
    return parse_mysql_connection(settings.database_url.get_secret_value())


def _resolve_package_path(settings: Settings, manager: AdminBackupManager, backup_id: str):
    manager.open_download(backup_id)  # 校验归属、成功状态与 SHA-256
    return package_path_for(settings.admin_backup_dir, backup_id)


def _run(args: argparse.Namespace, output: Callable[[str], None]) -> int:
    settings = _settings()
    manager = AdminBackupManager(settings)
    connection = _connection(settings)
    work_dir = settings.admin_backup_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "backup":
        if args.backup_command == "create":
            record = manager.create_backup(
                args.type,
                application_version=settings.app_version,
            )
            output(f"整库备份已完成：{record.backup_id}")
            output(f"类型：{record.backup_type}；大小：{record.size} 字节")
            return _report_offsite_result(record, output)
        if args.backup_command == "verify":
            report = manager.verify_backup(args.backup_id)
            output(f"备份 {report['backup_id']} 校验通过")
            output(f"数据库：{report['database']}；Schema：{report['schema_revision']}")
            output(f"表数量：{report['table_count']}；SQL SHA-256：{report['sql_sha256']}")
            return EXIT_SUCCESS
        if args.backup_command == "prune":
            result = manager.prune(dry_run=not args.execute)
            if result["dry_run"]:
                output(f"预览：将清理 {len(result['removed'])} 份，保留 {result['kept']} 份")
                for backup_id in result["removed"]:
                    output(f"  - {backup_id}")
            else:
                output(f"已清理 {len(result['removed'])} 份，保留 {result['kept']} 份")
            return EXIT_SUCCESS
        raise RuntimeError("未知备份子命令")

    if args.command == "restore":
        if args.restore_command == "prepare":
            package_path = _resolve_package_path(settings, manager, args.backup)
            parsed = parse_admin_backup(
                package_path,
                max_sql_bytes=settings.admin_backup_max_sql_bytes,
            )
            confirm = args.confirm_target
            if confirm is None:
                first = input("请输入目标库名：")
                second = input("请再次输入目标库名：")
                if first != second:
                    output("两次输入的目标库名不一致，已取消。")
                    return EXIT_INPUT_ERROR
                confirm = first
            with manager.lock:
                target = restore_prepare(
                    connection=connection,
                    package_path=package_path,
                    target=args.target,
                    confirm_target=confirm,
                    allow_test=args.allow_test_prefix,
                    mysql_path=settings.admin_backup_mysql_path,
                    work_dir=work_dir,
                    timeout=settings.admin_backup_command_timeout_seconds,
                    max_sql_bytes=settings.admin_backup_max_sql_bytes,
                )
            output(f"已恢复到新数据库：{target}")
            output("下一步请执行 restore verify，并在冒烟通过后由人工切换应用数据库。")
            return EXIT_SUCCESS
        if args.restore_command == "verify":
            package_path = _resolve_package_path(settings, manager, args.backup)
            parsed = parse_admin_backup(
                package_path,
                max_sql_bytes=settings.admin_backup_max_sql_bytes,
            )
            if args.confirm_smoke_complete:
                with manager.lock:
                    schema_head = verify_restored_schema_head(
                        connection=connection,
                        target=args.target,
                    )
                    manager._mark_restore_verified_unlocked(args.backup, args.target)
                output(f"恢复库 {args.target} 的当前结构复核通过：{schema_head}")
                output("已确认冒烟完成，该备份现已标记为恢复验证通过。")
                return EXIT_SUCCESS

            with manager.lock:
                verification = verify_restored_database(
                    connection=connection,
                    target=args.target,
                    expected_alembic_version=parsed.alembic_version,
                    expected_tables=parsed.tables,
                )
                schema_head = verify_restored_schema_head(
                    connection=connection,
                    target=args.target,
                )
                manager._mark_restore_structure_verified_unlocked(args.backup, args.target)
            output(f"恢复库 {verification.target} 核对通过")
            output(f"版本：{verification.alembic_version}；表数量：{verification.table_count}")
            output(f"Alembic 当前版本和 ORM 结构检查通过：{schema_head}")
            output("冒烟测试清单：")
            for index, step in enumerate(verification.smoke_checklist, start=1):
                output(f"  {index}. {step}")
            output("当前只完成结构核对；人工冒烟完成后请带 --confirm-smoke-complete 再次执行。")
            return EXIT_SUCCESS
        raise RuntimeError("未知恢复子命令")

    if args.command == "rehearsal":
        suffix = secrets.token_hex(4)
        target = f"{ALLOWED_TEST_PREFIX}{suffix}"
        output("开始隔离恢复演练")
        record = manager.create_backup(
            BACKUP_TYPE_REHEARSAL,
            application_version=settings.app_version,
        )
        output(f"已生成演练备份：{record.backup_id}")
        offsite_result = _report_offsite_result(record, output)
        if offsite_result != EXIT_SUCCESS:
            output("演练未继续：异地副本失败，本地备份已保留供排查。")
            return offsite_result
        package_path = _resolve_package_path(settings, manager, record.backup_id)
        parsed = parse_admin_backup(
            package_path,
            max_sql_bytes=settings.admin_backup_max_sql_bytes,
        )
        with manager.lock:
            try:
                restore_prepare(
                    connection=connection,
                    package_path=package_path,
                    target=target,
                    confirm_target=target,
                    allow_test=True,
                    mysql_path=settings.admin_backup_mysql_path,
                    work_dir=work_dir,
                    timeout=settings.admin_backup_command_timeout_seconds,
                    max_sql_bytes=settings.admin_backup_max_sql_bytes,
                )
                verification = verify_restored_database(
                    connection=connection,
                    target=target,
                    expected_alembic_version=parsed.alembic_version,
                    expected_tables=parsed.tables,
                )
                schema_head = verify_restored_schema_head(
                    connection=connection,
                    target=target,
                )
                manager._mark_restore_structure_verified_unlocked(record.backup_id, target)
                output(f"恢复到隔离测试库 {target} 并核对通过")
                output(f"版本：{verification.alembic_version}；表数量：{verification.table_count}")
                output(f"Alembic 当前版本和 ORM 结构检查通过：{schema_head}")
                run_restored_application_smoke(
                    connection=connection,
                    target=target,
                    connect_timeout=settings.database_connect_timeout_seconds,
                )
                output("登录、首页、查账、统计、记账和用户隔离冒烟通过")
            finally:
                drop_test_database(
                    connection=connection,
                    target=target,
                    mysql_path=settings.admin_backup_mysql_path,
                    work_dir=work_dir,
                    timeout=settings.admin_backup_command_timeout_seconds,
                )
        manager.mark_restore_verified(record.backup_id, target)
        output("隔离测试库已清理，完整恢复演练完成。")
        return EXIT_SUCCESS

    raise RuntimeError("未知命令")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args, print)
    except (TaskLockBusyError, BackupUnavailableError, RestoreError) as exc:
        print(f"操作未执行：{exc.message}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except BackupIndexCorruptedError as exc:
        print(f"操作未执行：{exc.message}", file=sys.stderr)
        return EXIT_SYSTEM_ERROR
    except DatabaseBackupError as exc:
        print(f"操作失败：{exc.message}", file=sys.stderr)
        return EXIT_SYSTEM_ERROR
    except (EOFError, KeyboardInterrupt):
        print("操作已取消，数据库未修改。", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except Exception:
        print("操作失败：发生未知错误，请检查本机数据库与备份工具配置。", file=sys.stderr)
        return EXIT_SYSTEM_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
