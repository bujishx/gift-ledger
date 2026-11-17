# ruff: noqa: RUF002
"""管理员整库备份的门面：编排转储、索引、异地复制与状态。

HTTP 接口和本机 CLI 都通过本模块操作整库备份，不直接拼凑底层细节。它负责：

- 受控目录布局与权限（目录 0700、文件 0600）；
- 同一时刻只运行一个备份／恢复任务的互斥锁；
- 生成备份时先读数据库元数据、再跑 mysqldump、计算 SHA-256、写严格版本化 manifest、
  原子改名成 ``.gladminbackup``，最后登记索引并触发异地加密复制；
- 任何失败都会在索引中记录一条「失败」条目（含脱敏中文摘要），保证管理员页面能同时看到
  最近成功和最近失败；
- 下载只返回「已完成且校验通过」的备份，返回前重新核对大小与 SHA-256。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.database import get_engine
from app.services.backup_index import (
    OFFSITE_COPIED,
    OFFSITE_FAILED,
    OFFSITE_NOT_CONFIGURED,
    BackupIndex,
    BackupIndexCorruptedError,
    BackupRecord,
    package_path_for,
    plan_retention,
)
from app.services.database_backup import (
    BACKUP_TYPES,
    ERROR_CONNECTION_UNAVAILABLE,
    FORMAT_NAME,
    FORMAT_VERSION,
    PACKAGE_SUFFIX,
    DatabaseBackupError,
    gather_database_metadata,
    get_mysqldump_version,
    gzip_file,
    new_backup_id,
    parse_admin_backup,
    parse_mysql_connection,
    run_mysqldump,
    sha256_file,
    write_admin_backup_package,
)
from app.services.offsite_replication import OffsiteReplicator
from app.services.task_lock import TaskLock


class BackupUnavailableError(Exception):
    """备份不存在、未完成或已损坏时抛出。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class BackupDownload:
    filename: str
    path: Path
    size: int


class AdminBackupManager:
    """整库备份门面。"""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.backup_dir = settings.admin_backup_dir
        self.timeout = settings.admin_backup_command_timeout_seconds
        self.index = BackupIndex(self.backup_dir)
        self.lock = TaskLock(self.backup_dir, stale_seconds=self.timeout + 3600)
        self.connection = (
            parse_mysql_connection(settings.database_url.get_secret_value())
            if settings.database_url is not None
            else None
        )
        self.offsite = OffsiteReplicator(
            offsite_dir=settings.admin_backup_offsite_dir,
            recipients=settings.admin_backup_age_recipient_list,
            age_path=settings.admin_backup_age_path,
            timeout=self.timeout,
        )

    # ---------------------------------------------------------------- 目录

    def _prepare_directory(self) -> None:
        if self.backup_dir.is_symlink():
            raise DatabaseBackupError("backup_dir_unsafe", "备份目录不可用")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.backup_dir, 0o700)

    def _engine(self):
        if self.settings.database_url is None:
            raise DatabaseBackupError(ERROR_CONNECTION_UNAVAILABLE, "尚未配置数据库地址")
        return get_engine(
            self.settings.database_url.get_secret_value(),
            self.settings.database_connect_timeout_seconds,
        )

    # ---------------------------------------------------------------- 创建

    def create_backup(self, backup_type: str, *, application_version: str) -> BackupRecord:
        if backup_type not in BACKUP_TYPES:
            raise DatabaseBackupError("backup_type_invalid", "备份类型不正确")
        if self.connection is None or not self.connection.database:
            raise DatabaseBackupError(
                ERROR_CONNECTION_UNAVAILABLE, "尚未配置数据库地址，无法生成整库备份"
            )

        backup_id = new_backup_id()
        created_at = datetime.now(UTC)
        failure_summary: str | None = None

        with self.lock:
            self._prepare_directory()
            raw_path = self.backup_dir / f"{backup_id}.sql.part"
            gz_path = self.backup_dir / f"{backup_id}.sql.gz"
            package_part = self.backup_dir / f"{backup_id}{PACKAGE_SUFFIX}.part"
            final_path: Path | None = None
            try:
                metadata = gather_database_metadata(self._engine())
                run_mysqldump(
                    self.connection,
                    database=self.connection.database,
                    mysqldump_path=self.settings.admin_backup_mysqldump_path,
                    output_path=raw_path,
                    work_dir=self.backup_dir,
                    timeout=self.timeout,
                )
                metadata_after = gather_database_metadata(self._engine())
                if metadata_after != metadata:
                    raise DatabaseBackupError(
                        "backup_source_changed",
                        "备份期间数据库数量或版本发生变化，请在较空闲时段重试",
                    )
                sql_sha256, sql_size = sha256_file(raw_path)
                gzip_file(raw_path, gz_path)
                manifest = {
                    "format": FORMAT_NAME,
                    "format_version": FORMAT_VERSION,
                    "backup_id": backup_id,
                    "backup_type": backup_type,
                    "created_at": created_at.isoformat(),
                    "database": metadata["database"],
                    "schema_revision": metadata["alembic_version"],
                    "application_version": application_version,
                    "mysqldump_version": get_mysqldump_version(
                        self.settings.admin_backup_mysqldump_path, timeout=self.timeout
                    ),
                    "sql_sha256": sql_sha256,
                    "sql_size": sql_size,
                    "alembic_version": metadata["alembic_version"],
                    "tables": metadata["tables"],
                }
                write_admin_backup_package(
                    manifest=manifest,
                    gzip_sql_path=gz_path,
                    output_path=package_part,
                )
                package_sha256, package_size = sha256_file(package_part)
                final_path = package_path_for(self.backup_dir, backup_id)
                if final_path.exists() or final_path.is_symlink():
                    raise DatabaseBackupError("backup_id_collision", "备份编号冲突，请重试")
                os.replace(package_part, final_path)
                os.chmod(final_path, 0o600)
                offsite = self.offsite.replicate(
                    backup_id=backup_id,
                    package_path=final_path,
                    work_dir=self.backup_dir,
                )
            except DatabaseBackupError as exc:
                failure_summary = exc.message
                self._record_failure(backup_id, backup_type, created_at, failure_summary)
                raise
            except OSError:
                failure_summary = "备份文件处理失败"
                self._unlink_quietly(final_path)
                self._record_failure(backup_id, backup_type, created_at, failure_summary)
                raise DatabaseBackupError("backup_file_operation_failed", failure_summary) from None
            finally:
                self._unlink_quietly(raw_path)
                self._unlink_quietly(gz_path)
                self._unlink_quietly(package_part)

            record = BackupRecord(
                backup_id=backup_id,
                backup_type=backup_type,
                created_at=created_at,
                completed_at=datetime.now(UTC),
                size=package_size,
                sha256=package_sha256,
                database=metadata["database"],
                schema_revision=metadata["alembic_version"],
                success=True,
                verified=False,
                offsite_status=offsite.status,
                offsite_sha256=offsite.sha256,
                failure_summary=(
                    "本地备份成功，但异地加密副本失败" if offsite.status == OFFSITE_FAILED else None
                ),
            )
            self._add_record(record)
            return record

    def _record_failure(
        self,
        backup_id: str,
        backup_type: str,
        created_at: datetime,
        failure_summary: str,
    ) -> None:
        record = BackupRecord(
            backup_id=backup_id,
            backup_type=backup_type,
            created_at=created_at,
            completed_at=datetime.now(UTC),
            size=0,
            sha256="0" * 64,
            database=self.connection.database or "",
            schema_revision="",
            success=False,
            verified=False,
            offsite_status=OFFSITE_NOT_CONFIGURED,
            offsite_sha256=None,
            failure_summary=failure_summary,
        )
        self._add_record(record)

    def _add_record(self, record: BackupRecord) -> None:
        try:
            records = self.index.load()
        except BackupIndexCorruptedError:
            raise DatabaseBackupError(
                "backup_index_corrupted", "备份索引已损坏，无法登记备份"
            ) from None
        records = [existing for existing in records if existing.backup_id != record.backup_id]
        records.append(record)
        self.index.save(records)

    # ---------------------------------------------------------------- 状态

    def status(self) -> dict:
        records = self.index.load()
        records_sorted = sorted(records, key=lambda r: (r.created_at, r.backup_id), reverse=True)
        recent_success = next((r for r in records_sorted if r.success), None)
        recent_failure = next((r for r in records_sorted if not r.success), None)
        recent_offsite_failure = next(
            (r for r in records_sorted if r.offsite_status == OFFSITE_FAILED), None
        )
        successful_records = [r for r in records_sorted if r.success]
        latest_offsite_attempt = next(
            (r for r in successful_records if r.offsite_status in {OFFSITE_COPIED, OFFSITE_FAILED}),
            None,
        )
        latest_offsite_success = next(
            (r for r in successful_records if r.offsite_status == OFFSITE_COPIED), None
        )

        now = datetime.now(UTC)
        if recent_success is not None and recent_success.completed_at is not None:
            overdue = now - recent_success.completed_at > timedelta(
                hours=self.settings.admin_backup_stale_overdue_hours
            )
        else:
            overdue = True

        offsite_configured = self.offsite.configured()
        if offsite_configured:
            offsite_overdue = (
                latest_offsite_success is None
                or latest_offsite_success.completed_at is None
                or now - latest_offsite_success.completed_at
                > timedelta(hours=self.settings.admin_backup_stale_overdue_hours)
            )
            offsite_healthy = (
                latest_offsite_attempt is not None
                and latest_offsite_attempt.offsite_status == OFFSITE_COPIED
                and not offsite_overdue
            )
        else:
            offsite_overdue = False
            offsite_healthy = None

        return {
            "backups": [_public_record(r) for r in records_sorted],
            "recent_success": _public_record(recent_success) if recent_success else None,
            "recent_failure": _public_record(recent_failure) if recent_failure else None,
            "recent_offsite_failure": (
                _public_record(recent_offsite_failure) if recent_offsite_failure else None
            ),
            "latest_offsite_success": (
                _public_record(latest_offsite_success) if latest_offsite_success else None
            ),
            "latest_offsite_attempt": (
                _public_record(latest_offsite_attempt) if latest_offsite_attempt else None
            ),
            "offsite_configured": offsite_configured,
            "offsite_healthy": offsite_healthy,
            "offsite_overdue": offsite_overdue,
            "overdue": overdue,
            "overall_healthy": not overdue and (not offsite_configured or bool(offsite_healthy)),
            "retention": {
                "keep_daily": self.settings.admin_backup_keep_daily,
                "keep_weekly": self.settings.admin_backup_keep_weekly,
                "keep_monthly": self.settings.admin_backup_keep_monthly,
            },
        }

    # ---------------------------------------------------------------- 下载

    def open_download(self, backup_id: str) -> BackupDownload:
        records = self.index.load()
        record = next((r for r in records if r.backup_id == backup_id), None)
        if record is None:
            raise BackupUnavailableError("BACKUP_NOT_FOUND", "备份不存在")
        if not record.success:
            raise BackupUnavailableError("BACKUP_NOT_READY", "备份尚未完成或已失败")
        package = package_path_for(self.backup_dir, backup_id)
        resolved_dir = self.backup_dir.resolve()
        if package.resolve().parent != resolved_dir:
            raise BackupUnavailableError("BACKUP_NOT_FOUND", "备份不存在")
        if package.is_symlink():
            raise BackupUnavailableError("BACKUP_NOT_FOUND", "备份不存在")
        try:
            package_sha256, package_size = sha256_file(package)
        except OSError as exc:
            raise BackupUnavailableError("BACKUP_NOT_FOUND", "备份不存在") from exc
        if package_size != record.size or package_sha256 != record.sha256:
            raise BackupUnavailableError("BACKUP_CORRUPTED", "备份文件已损坏")
        filename = f"gift-ledger-admin-backup-{backup_id}{PACKAGE_SUFFIX}"
        return BackupDownload(filename=filename, path=package, size=package_size)

    # ---------------------------------------------------------------- 校验

    def verify_backup(self, backup_id: str) -> dict:
        with self.lock:
            download = self.open_download(backup_id)
            parsed = parse_admin_backup(
                download.path,
                max_sql_bytes=self.settings.admin_backup_max_sql_bytes,
            )
        return {
            "backup_id": backup_id,
            "format": parsed.format_name,
            "database": parsed.database,
            "schema_revision": parsed.schema_revision,
            "alembic_version": parsed.alembic_version,
            "sql_sha256": parsed.sql_sha256,
            "table_count": len(parsed.tables),
            "integrity_verified": True,
        }

    def mark_restore_structure_verified(self, backup_id: str, target: str) -> None:
        """记录某个恢复目标已完成原始行数、版本和结构核对。"""

        with self.lock:
            self._mark_restore_structure_verified_unlocked(backup_id, target)

    def _target_record(self, backup_id: str) -> tuple[list[BackupRecord], BackupRecord]:
        records = self.index.load()
        target = next((r for r in records if r.backup_id == backup_id), None)
        if target is None or not target.success:
            raise BackupUnavailableError("BACKUP_NOT_FOUND", "备份不存在")
        return records, target

    def _mark_restore_structure_verified_unlocked(self, backup_id: str, target_name: str) -> None:
        records, target = self._target_record(backup_id)
        updated = [r for r in records if r.backup_id != backup_id]
        updated.append(
            replace(
                target,
                verified=False,
                restore_target=target_name,
                restore_structure_verified_at=datetime.now(UTC),
            )
        )
        self.index.save(updated)

    def mark_restore_verified(self, backup_id: str, target: str) -> None:
        """仅允许已完成同一目标精确核对的备份登记冒烟验证成功。"""

        with self.lock:
            self._mark_restore_verified_unlocked(backup_id, target)

    def _mark_restore_verified_unlocked(self, backup_id: str, target_name: str) -> None:
        records, target = self._target_record(backup_id)
        if target.restore_target != target_name or target.restore_structure_verified_at is None:
            raise BackupUnavailableError(
                "RESTORE_STRUCTURE_NOT_VERIFIED",
                "必须先对同一个恢复目标完成精确核对，才能登记冒烟完成",
            )
        updated = [r for r in records if r.backup_id != backup_id]
        updated.append(replace(target, verified=True))
        self.index.save(updated)

    # ---------------------------------------------------------------- 保留

    def prune(self, *, dry_run: bool) -> dict:
        with self.lock:
            return self._prune_unlocked(dry_run=dry_run)

    def _prune_unlocked(self, *, dry_run: bool) -> dict:
        records = self.index.load()
        keep_ids = plan_retention(
            records,
            keep_daily=self.settings.admin_backup_keep_daily,
            keep_weekly=self.settings.admin_backup_keep_weekly,
            keep_monthly=self.settings.admin_backup_keep_monthly,
        )
        failed_automatic = [r for r in records if r.backup_type == "automatic" and not r.success]
        newest_failure = (
            max(failed_automatic, key=lambda r: (r.created_at, r.backup_id))
            if failed_automatic
            else None
        )
        removed: list[str] = []
        kept_records: list[BackupRecord] = []
        for record in records:
            if record.backup_type != "automatic":
                kept_records.append(record)
                continue
            if record.backup_id in keep_ids:
                kept_records.append(record)
                continue
            if newest_failure is not None and record.backup_id == newest_failure.backup_id:
                kept_records.append(record)
                continue
            removed.append(record.backup_id)

        if not dry_run:
            by_id = {record.backup_id: record for record in records}
            for backup_id in removed:
                record = by_id[backup_id]
                if record.offsite_status == "copied":
                    self.offsite.delete(backup_id)
                self.index.safe_delete_package(backup_id)
            self.index.save(kept_records)

        return {
            "dry_run": dry_run,
            "removed": removed,
            "kept": len(kept_records),
        }

    def _unlink_quietly(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _public_record(record: BackupRecord) -> dict:
    return {
        "backup_id": record.backup_id,
        "backup_type": record.backup_type,
        "created_at": record.created_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "size": record.size,
        "database": record.database,
        "schema_revision": record.schema_revision,
        "success": record.success,
        "verified": record.verified,
        "offsite_status": record.offsite_status,
        "failure_summary": record.failure_summary,
    }


__all__ = [
    "AdminBackupManager",
    "BackupDownload",
    "BackupUnavailableError",
]
