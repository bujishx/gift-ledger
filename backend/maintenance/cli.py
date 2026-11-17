"""Common maintenance commands, deliberately limited to isolated rehearsals.

Run with ``python3.12 -m maintenance.cli --config ... --result ... <command>``.
No command changes the live deployment, drops databases, or cleans old backups.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL

from app.core.config import Settings
from app.services.admin_backup import AdminBackupManager
from app.services.database_backup import (
    BACKUP_TYPE_REHEARSAL,
    DatabaseBackupError,
    parse_admin_backup,
    run_mysql_import,
    run_mysql_script,
    run_mysqldump,
    sha256_file,
)
from app.services.database_restore import (
    RestoreError,
    restore_prepare,
    verify_restored_database,
    verify_restored_schema_head,
)
from maintenance.database import (
    CURRENT_REVISION,
    DATABASE,
    SCOPE,
    MaintenanceConfig,
    MaintenanceError,
    database_fingerprint,
    guarded_engine,
    load_config,
    read_json,
    require_guard,
    verify_relationships,
    verify_safety_owners,
    verify_schema,
)
from maintenance.recovery_set import RecoverySetError


def work_path(config: MaintenanceConfig, path: Path, *, existing: bool = False) -> Path:
    return _bounded_path(config.work_root, path, existing=existing)


def _bounded_path(work_root: Path, path: Path, *, existing: bool = False) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise MaintenanceError("PATH_FORBIDDEN", "维护文件必须使用绝对路径")
    root = work_root.resolve()
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise MaintenanceError("PATH_FORBIDDEN", "维护文件必须位于独立演练工作目录")
    if any(part.is_symlink() for part in [path, *path.parents] if part != root):
        raise MaintenanceError("PATH_FORBIDDEN", "维护文件路径不能包含符号链接")
    if existing and (not path.is_file() or path.is_symlink()):
        raise MaintenanceError("PATH_UNAVAILABLE", "演练输入文件不可用")
    return path


def atomic_result(path: Path, value: dict) -> None:
    """Publish a new result atomically, without replacing prior evidence."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=".maintenance-result-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def operation_lock(config: MaintenanceConfig):
    """Never automatically steal a stale lock or delete somebody else's lock."""
    config.work_root.mkdir(parents=True, exist_ok=True)
    path = config.work_root / ".maintenance.lock"
    token = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise MaintenanceError("TASK_BUSY", "已有维护任务或未解除的任务锁") from None
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        try:
            if path.read_text(encoding="ascii") == token:
                path.unlink()
        except OSError:
            pass


def _record_dict(record) -> dict:
    value = asdict(record)
    for key, item in value.items():
        if isinstance(item, datetime):
            value[key] = item.isoformat()
    return value


def _file_mapping(config: MaintenanceConfig, filename: Path) -> dict[str, Path]:
    mapping = read_json(work_path(config, filename, existing=True))
    if not isinstance(mapping, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
    ):
        raise MaintenanceError("CONFIG_INVALID", "资料集文件清单必须为名称到文件的映射")
    return {key: work_path(config, Path(value), existing=True) for key, value in mapping.items()}


def _settings(config: MaintenanceConfig) -> Settings:
    connection = config.connection
    url = URL.create("mysql+pymysql", username=connection.user, password=connection.password,
                     host=connection.host, port=connection.port, database=DATABASE,
                     query={"charset": "utf8mb4"})
    # Explicit settings avoid using the live app's environment or .env file.
    return Settings(
        _env_file=None, environment="production", database_url=SecretStr(url.render_as_string(
            hide_password=False)), jwt_secret=SecretStr(secrets.token_urlsafe(48)),
        trusted_origins="https://localhost:18443", auth_cookie_secure=True,
        admin_backup_dir=config.work_root / "database-backups",
        admin_backup_offsite_dir=None, admin_backup_age_recipients="",
        admin_backup_command_timeout_seconds=180,
        admin_backup_mysql_path="mysql", admin_backup_mysqldump_path="mysqldump",
        import_temp_dir=config.work_root / "transient-imports",
        personal_restore_temp_dir=config.work_root / "transient-restores",
        personal_safety_backup_dir=config.work_root / "personal-safety-backups",
    )


def _backup(config: MaintenanceConfig, args) -> dict:
    from maintenance.recovery_set import (
        build_recovery_set,
        normalize_backup_record,
        verify_recovery_set,
    )

    if config.role != "source" or config.user != "maintenance_backup":
        raise MaintenanceError("ROLE_FORBIDDEN", "备份仅允许独立源实例的只读维护账号")
    provenance = read_json(work_path(config, args.provenance, existing=True))
    config_files = _file_mapping(config, args.config_files)
    app_files = _file_mapping(config, args.app_files)
    output_dir = work_path(config, args.output_dir)
    with guarded_engine(config, DATABASE) as engine:
        with engine.connect() as connection:
            if int(connection.execute(text("SELECT @@global.read_only")).scalar_one()) != 1:
                raise MaintenanceError("SOURCE_NOT_FROZEN", "备份前必须将合成源实例设为只读")
        verify_schema(engine, config)
        verify_relationships(engine, config)
        verify_safety_owners(engine, config, [
            path for name, path in app_files.items()
            if name.startswith("personal-safety-backups/") and name.endswith(".json")
        ])
        before = database_fingerprint(engine, config)
        manager = AdminBackupManager(_settings(config))
        record = manager.create_backup(BACKUP_TYPE_REHEARSAL, application_version="1.5.0")
        require_guard(engine, config)
        after = database_fingerprint(engine, config)
        with engine.connect() as connection:
            read_only = int(connection.execute(text("SELECT @@global.read_only")).scalar_one())
        if after != before or read_only != 1:
            raise MaintenanceError("SOURCE_CHANGED", "备份期间源实例或全列内容发生变化")
        package = manager.backup_dir / f"{record.backup_id}.gladminbackup"
        manager.verify_backup(record.backup_id)
        # This self-contained set carries this package only. A new portable
        # index must not refer to older packages absent from the set, nor inherit
        # another instance's restoration or offsite verification claims.
        portable_record = normalize_backup_record(_record_dict(record))
        portable_index = config.work_root / "export-indices" / record.backup_id / "index.json"
        atomic_result(portable_index, {"version": 2, "entries": [portable_record]})
        app_files["admin-backups/index.json"] = portable_index
        result_path = build_recovery_set(
            output_dir=output_dir, package_path=package, record=portable_record,
            provenance=provenance, database_fingerprint=before, config_files=config_files,
            app_files=app_files,
        )
        verify_recovery_set(result_path)
    return {"recovery_set": str(result_path), "backup_id": record.backup_id,
            "source_frozen": True, "fingerprint": before,
            "local_complete": True, "offsite_configured": False,
            "restore_verified": False}


def _verify_database(config: MaintenanceConfig, database: str, expected: dict | None) -> dict:
    with guarded_engine(config, database) as engine:
        revision = verify_schema(engine, config)
        fingerprint = database_fingerprint(engine, config)
        relations = verify_relationships(engine, config)
    if expected is not None and fingerprint != expected:
        raise MaintenanceError("DATA_MISMATCH", "恢复库全列内容或字典与备份源不一致")
    return {"database": database, "schema_revision": revision,
            "fingerprint": fingerprint, "relationships": relations}


@contextmanager
def _extracted_set(config: MaintenanceConfig, path: Path):
    from maintenance.recovery_set import extract_recovery_set

    archive = work_path(config, path, existing=True)
    parent = config.work_root / "extracted-sets"
    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    target = parent / secrets.token_hex(16)
    result = extract_recovery_set(archive, target)
    # Evidence and inputs are retained after success/failure. No automatic deletion.
    yield result


def _restore(config: MaintenanceConfig, args) -> dict:
    if config.role != "restore":
        raise MaintenanceError("ROLE_FORBIDDEN", "恢复仅允许独立恢复实例")
    if args.target != config.scratch_database or args.confirm_target != args.target:
        raise MaintenanceError("TARGET_FORBIDDEN", "恢复目标必须与本次演练编号精确匹配")
    with guarded_engine(config) as engine, _extracted_set(config, args.recovery_set) as archive:
        package = archive["package_path"]
        parsed = parse_admin_backup(package)
        if parsed.database != DATABASE or parsed.schema_revision != CURRENT_REVISION:
            raise MaintenanceError("VERSION_INCOMPATIBLE", "资料集数据库版本不受支持")
        require_guard(engine, config)
        work_dir = config.work_root / "restore-work" / secrets.token_hex(16)
        work_dir.mkdir(parents=True, mode=0o700)
        target = restore_prepare(
            connection=config.connection, package_path=package, target=args.target,
            confirm_target=args.confirm_target, allow_test=False, mysql_path="mysql",
            work_dir=work_dir, timeout=180,
        )
        require_guard(engine, config)
        verify_restored_database(connection=config.connection, target=target,
                                 expected_alembic_version=parsed.alembic_version,
                                 expected_tables=parsed.tables)
        verify_restored_schema_head(connection=config.connection, target=target)
        details = _verify_database(config, target, archive["database_fingerprint"])
    return {**details, "prepared": True, "promoted": False,
            "files_extracted_to": str(archive["root"])}


def _promote(config: MaintenanceConfig, args) -> dict:
    if config.role != "restore" or args.scratch != config.scratch_database:
        raise MaintenanceError("ROLE_FORBIDDEN", "提升仅允许本次独立恢复库")
    if args.confirm_target != DATABASE:
        raise MaintenanceError("TARGET_CONFIRM_MISMATCH", "必须明确确认固定应用库名")
    app_data = work_path(config, args.app_data)
    if app_data != config.work_root / "restore-app-data":
        raise MaintenanceError("PATH_FORBIDDEN", "只能恢复到独立候选应用数据卷")
    with guarded_engine(config) as engine, _extracted_set(config, args.recovery_set) as archive:
        _verify_database(config, args.scratch, archive["database_fingerprint"])
        install_files = {
            name: archive["root"] / member for name, member in archive["app_files"].items()
        }
        install_files[f"admin-backups/{archive['record']['backup_id']}.gladminbackup"] = (
            archive["package_path"]
        )
        safety_metadata = [
            path for name, path in install_files.items()
            if name.startswith("personal-safety-backups/") and name.endswith(".json")
        ]
        if safety_metadata:
            with guarded_engine(config, args.scratch) as scratch_engine:
                verify_safety_owners(scratch_engine, config, safety_metadata)
        for name in install_files:
            target_file = work_path(config, app_data / name)
            if target_file.exists() or target_file.is_symlink():
                raise MaintenanceError("APP_FILE_EXISTS", "候选应用文件已经存在，拒绝覆盖")
        if app_data.exists() and any(app_data.iterdir()):
            raise MaintenanceError("APP_TARGET_NOT_EMPTY", "候选应用数据卷必须完全为空")
        require_guard(engine, config)
        with engine.connect() as connection:
            exists = connection.execute(text(
                "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=:name"
            ), {"name": DATABASE}).scalar_one()
        if exists:
            raise MaintenanceError("TARGET_EXISTS", "固定应用库已经存在，绝不覆盖或删除")
        work_dir = config.work_root / "promote-work" / secrets.token_hex(16)
        work_dir.mkdir(parents=True, mode=0o700)
        dump = work_dir / "validated-scratch.sql"
        require_guard(engine, config)
        run_mysqldump(config.connection, database=args.scratch, mysqldump_path="mysqldump",
                      output_path=dump, work_dir=work_dir, timeout=180)
        require_guard(engine, config)
        run_mysql_script(config.connection, mysql_path="mysql", statement=(
            f"CREATE DATABASE `{DATABASE}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
        ), work_dir=work_dir, timeout=180)
        with dump.open("rb") as stream:
            run_mysql_import(config.connection, database=DATABASE, mysql_path="mysql",
                             sql_stream=stream, work_dir=work_dir, timeout=180)
        require_guard(engine, config)
        details = _verify_database(config, DATABASE, archive["database_fingerprint"])
        copied_files = _install_files(app_data, install_files)
    return {**details, "promoted": True, "existing_database_overwritten": False,
            "deployment_switched": False, "files_extracted_to": str(archive["root"]),
            "application_files_restored": copied_files}


def _install_files(destination: Path, files: dict[str, Path]) -> dict:
    """Publish into the new candidate volume only after all DB checks pass."""
    result = {}
    for name, source in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_fd, temporary_name = tempfile.mkstemp(prefix=".restore-file-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(temporary_fd, "wb") as output, source.open("rb") as incoming:
                shutil.copyfileobj(incoming, output, length=1024**2)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            if sha256_file(temporary) != sha256_file(source):
                raise MaintenanceError("APP_FILE_MISMATCH", "候选应用文件复制校验失败")
            os.link(temporary, target)
            result[name] = {"sha256": sha256_file(target)[0], "size": target.stat().st_size}
        finally:
            temporary.unlink(missing_ok=True)
    # The unchanged API image runs as UID 10001. Private restored files must be
    # usable by that account, rather than accidentally owned by maintenance root.
    for directory in [destination, *(path for path in destination.rglob("*") if path.is_dir())]:
        os.chown(directory, 10001, 0)
        os.chmod(directory, 0o700)
    for name in files:
        os.chown(destination / name, 10001, 0)
        os.chmod(destination / name, 0o600)
    return result


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="独立合成测试的备份和恢复维护工具")
    command.add_argument("--config", type=Path, required=True)
    command.add_argument("--result", type=Path, required=True)
    commands = command.add_subparsers(dest="command", required=True)
    commands.add_parser("guard")
    commands.add_parser("fingerprint")
    backup = commands.add_parser("backup")
    backup.add_argument("--provenance", type=Path, required=True)
    backup.add_argument("--config-files", type=Path, required=True)
    backup.add_argument("--app-files", type=Path, required=True)
    backup.add_argument("--output-dir", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--database", default=DATABASE)
    verify.add_argument("--recovery-set", type=Path)
    verify_set = commands.add_parser("verify-set")
    verify_set.add_argument("--recovery-set", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--recovery-set", type=Path, required=True)
    restore.add_argument("--target", required=True)
    restore.add_argument("--confirm-target", required=True)
    promote = commands.add_parser("promote")
    promote.add_argument("--recovery-set", type=Path, required=True)
    promote.add_argument("--scratch", required=True)
    promote.add_argument("--confirm-target", required=True)
    promote.add_argument("--app-data", type=Path, required=True)
    encrypt = commands.add_parser("encrypt")
    encrypt.add_argument("--recovery-set", type=Path, required=True)
    encrypt.add_argument("--recipient-file", type=Path, required=True)
    encrypt.add_argument("--output", type=Path, required=True)
    decrypt = commands.add_parser("decrypt")
    decrypt.add_argument("--encrypted-set", type=Path, required=True)
    decrypt.add_argument("--identity-file", type=Path, required=True)
    decrypt.add_argument("--output", type=Path, required=True)
    return command


def execute(config: MaintenanceConfig, args) -> dict:
    if args.command == "guard":
        with guarded_engine(config) as engine:
            return require_guard(engine, config)
    if args.command == "fingerprint":
        with guarded_engine(config, DATABASE) as engine:
            return {"fingerprint": database_fingerprint(engine, config)}
    if args.command == "backup":
        return _backup(config, args)
    if args.command == "restore":
        return _restore(config, args)
    if args.command == "promote":
        return _promote(config, args)
    if args.command == "verify":
        expected = None
        if args.recovery_set is not None:
            from maintenance.recovery_set import verify_recovery_set

            archive = verify_recovery_set(work_path(config, args.recovery_set, existing=True))
            expected = archive["database_fingerprint"]
        return _verify_database(config, args.database, expected)
    from maintenance.recovery_set import (
        decrypt_recovery_set,
        encrypt_recovery_set,
        verify_recovery_set,
    )

    if args.command == "verify-set":
        archive = verify_recovery_set(work_path(config, args.recovery_set, existing=True))
        return {"backup_id": archive["record"]["backup_id"],
                "provenance": archive["provenance"],
                "fingerprint": archive["database_fingerprint"], "package_verified": True}
    if args.command == "encrypt":
        recipient_path = work_path(config, args.recipient_file, existing=True)
        path = encrypt_recovery_set(
            work_path(config, args.recovery_set, existing=True),
            recipient_path, work_path(config, args.output),
        )
        return {"encrypted_recovery_set": str(path), "offsite_configured": False}
    if args.command == "decrypt":
        path = decrypt_recovery_set(
            work_path(config, args.encrypted_set, existing=True),
            work_path(config, args.identity_file, existing=True),
            work_path(config, args.output),
        )
        return {"decrypted_recovery_set": str(path), "package_verified": True}
    raise MaintenanceError("COMMAND_UNKNOWN", "维护命令不受支持")


def main(argv: list[str] | None = None) -> int:
    import json

    args = parser().parse_args(argv)
    started = datetime.now(UTC).isoformat()
    result_path = None
    config = None
    try:
        # Even a rejected config gets a failure record. The path boundary is
        # fixed independently of that untrusted config and cannot select a
        # production location, or replace earlier evidence.
        if os.environ.get("MAINTENANCE_SCOPE") == SCOPE:
            result_path = _bounded_path(Path("/work"), args.result)
        config = load_config(args.config)
        result_path = work_path(config, args.result)
        if result_path.exists() or result_path.is_symlink():
            raise MaintenanceError("RESULT_EXISTS", "操作记录已经存在，不能覆盖历史证据")
        with operation_lock(config):
            details = execute(config, args)
        result = {"format": "gift-ledger-maintenance-result", "version": 1,
                  "operation": args.command, "run_id": config.run_id, "role": config.role,
                  "started_at": started, "completed_at": datetime.now(UTC).isoformat(),
                  "success": True, "details": details}
        atomic_result(result_path, result)
        print(json.dumps({"success": True, "operation": args.command,
                          "result": str(result_path)}, ensure_ascii=False))
        return 0
    except Exception as error:
        if isinstance(error, (
            MaintenanceError, DatabaseBackupError, RestoreError, RecoverySetError,
        )):
            code, message = error.code, error.message
        else:
            code, message = "MAINTENANCE_FAILED", "维护操作失败，未完成验收"
        failure = {"format": "gift-ledger-maintenance-result", "version": 1,
                   "operation": args.command, "started_at": started,
                   "completed_at": datetime.now(UTC).isoformat(), "success": False,
                   "code": code, "message": message}
        if config is not None:
            failure.update(run_id=config.run_id, role=config.role)
        if result_path is not None and not result_path.exists() and not result_path.is_symlink():
            try:
                atomic_result(result_path, failure)
            except OSError:
                pass
        print(json.dumps({"success": False, "code": code, "message": message},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
