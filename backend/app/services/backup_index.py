# ruff: noqa: RUF002
"""整库备份的受控 JSON 索引、状态与分层保留。

索引文件记录了每份备份的编号、类型、时间、大小、SHA-256、数据库逻辑名、Schema／Alembic
版本、成功／失败、验证状态、异地复制状态和脱敏后的中文失败摘要。索引使用原子写入，损坏时
安全失败（绝不据此误删备份文件）。

保留算法只处理索引中已确认、且位于受控目录内的「自动备份」，边界使用 UTC，结果确定可测试；
手工、维护前和演练备份永不自动删除。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.database_backup import (
    BACKUP_TYPE_AUTOMATIC,
    PACKAGE_SUFFIX,
)

INDEX_FILENAME = "index.json"
INDEX_VERSION = 2
INDEX_KEYS = {"version", "entries"}

LEGACY_RECORD_KEYS = {
    "backup_id",
    "backup_type",
    "created_at",
    "completed_at",
    "size",
    "sha256",
    "database",
    "schema_revision",
    "success",
    "verified",
    "offsite_status",
    "offsite_sha256",
    "failure_summary",
}
RECORD_KEYS = LEGACY_RECORD_KEYS | {
    "restore_target",
    "restore_structure_verified_at",
}

OFFSITE_NOT_CONFIGURED = "not_configured"
OFFSITE_COPIED = "copied"
OFFSITE_FAILED = "failed"
OFFSITE_STATUSES = frozenset({OFFSITE_NOT_CONFIGURED, OFFSITE_COPIED, OFFSITE_FAILED})

_BACKUP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class BackupIndexCorruptedError(RuntimeError):
    """索引文件损坏或格式错误时抛出；调用方必须安全失败，不得据此清理备份。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class BackupRecord:
    """索引中的单条备份记录。"""

    backup_id: str
    backup_type: str
    created_at: datetime
    completed_at: datetime | None
    size: int
    sha256: str
    database: str
    schema_revision: str
    success: bool
    verified: bool
    offsite_status: str
    offsite_sha256: str | None
    failure_summary: str | None
    restore_target: str | None = None
    restore_structure_verified_at: datetime | None = None


def _parse_datetime(value: Any, field: str) -> datetime:
    if type(value) is not str:
        raise BackupIndexCorruptedError(f"索引字段 {field} 格式错误")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BackupIndexCorruptedError(f"索引字段 {field} 格式错误") from exc
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise BackupIndexCorruptedError(f"索引字段 {field} 必须使用 UTC")
    return parsed


def _parse_record(value: Any, *, version: int) -> BackupRecord:
    expected_keys = LEGACY_RECORD_KEYS if version == 1 else RECORD_KEYS
    if type(value) is not dict or set(value) != expected_keys:
        raise BackupIndexCorruptedError("索引条目字段集合不正确")
    backup_id = value["backup_id"]
    if type(backup_id) is not str or _BACKUP_ID_PATTERN.fullmatch(backup_id) is None:
        raise BackupIndexCorruptedError("索引条目备份编号不正确")
    backup_type = value["backup_type"]
    if type(backup_type) is not str or backup_type not in {
        "automatic",
        "manual",
        "pre-maintenance",
        "rehearsal",
    }:
        raise BackupIndexCorruptedError("索引条目备份类型不正确")
    size = value["size"]
    if type(size) is not int or type(size) is bool or size < 0:
        raise BackupIndexCorruptedError("索引条目大小不正确")
    sha256_value = value["sha256"]
    if type(sha256_value) is not str or _SHA256_PATTERN.fullmatch(sha256_value) is None:
        raise BackupIndexCorruptedError("索引条目校验值不正确")
    if type(value["success"]) is not bool or type(value["verified"]) is not bool:
        raise BackupIndexCorruptedError("索引条目状态字段不正确")
    offsite_status = value["offsite_status"]
    if type(offsite_status) is not str or offsite_status not in OFFSITE_STATUSES:
        raise BackupIndexCorruptedError("索引条目异地状态不正确")
    offsite_sha256 = value["offsite_sha256"]
    if offsite_sha256 is not None and (
        type(offsite_sha256) is not str or _SHA256_PATTERN.fullmatch(offsite_sha256) is None
    ):
        raise BackupIndexCorruptedError("索引条目异地校验值不正确")
    failure_summary = value["failure_summary"]
    if failure_summary is not None and type(failure_summary) is not str:
        raise BackupIndexCorruptedError("索引条目失败摘要不正确")
    if type(value["database"]) is not str or type(value["schema_revision"]) is not str:
        raise BackupIndexCorruptedError("索引条目数据库信息不正确")
    if not value["success"] and value["verified"]:
        raise BackupIndexCorruptedError("失败备份不能标记为已验证")
    restore_target = value.get("restore_target")
    if restore_target is not None and type(restore_target) is not str:
        raise BackupIndexCorruptedError("索引条目恢复目标不正确")
    restore_structure_verified_at = value.get("restore_structure_verified_at")
    if restore_structure_verified_at is not None:
        restore_structure_verified_at = _parse_datetime(
            restore_structure_verified_at,
            "restore_structure_verified_at",
        )
    if (
        version == INDEX_VERSION
        and value["verified"]
        and (restore_target is None or restore_structure_verified_at is None)
    ):
        raise BackupIndexCorruptedError("已验证备份缺少恢复核对记录")

    verified = value["verified"] if version == INDEX_VERSION else False
    return BackupRecord(
        backup_id=backup_id,
        backup_type=backup_type,
        created_at=_parse_datetime(value["created_at"], "created_at"),
        completed_at=(
            _parse_datetime(value["completed_at"], "completed_at")
            if value["completed_at"] is not None
            else None
        ),
        size=size,
        sha256=sha256_value,
        database=value["database"],
        schema_revision=value["schema_revision"],
        success=value["success"],
        verified=verified,
        offsite_status=offsite_status,
        offsite_sha256=offsite_sha256,
        failure_summary=failure_summary,
        restore_target=restore_target,
        restore_structure_verified_at=restore_structure_verified_at,
    )


def _serialize_record(record: BackupRecord) -> dict:
    return {
        "backup_id": record.backup_id,
        "backup_type": record.backup_type,
        "created_at": record.created_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "size": record.size,
        "sha256": record.sha256,
        "database": record.database,
        "schema_revision": record.schema_revision,
        "success": record.success,
        "verified": record.verified,
        "offsite_status": record.offsite_status,
        "offsite_sha256": record.offsite_sha256,
        "failure_summary": record.failure_summary,
        "restore_target": record.restore_target,
        "restore_structure_verified_at": (
            record.restore_structure_verified_at.isoformat()
            if record.restore_structure_verified_at
            else None
        ),
    }


def _backup_sort_key(record: BackupRecord) -> tuple:
    return (record.created_at, record.backup_id)


def _newest_per_bucket(
    records: list[BackupRecord],
    *,
    key_fn,
    limit: int,
) -> set[str]:
    """按桶分组后取每桶最新的一份，再按桶键倒序保留最近 ``limit`` 个桶。"""
    buckets: dict[Any, list[BackupRecord]] = {}
    for record in records:
        buckets.setdefault(key_fn(record), []).append(record)
    representatives: list[tuple[Any, BackupRecord]] = []
    for key, items in buckets.items():
        representatives.append((key, max(items, key=_backup_sort_key)))
    representatives.sort(key=lambda pair: pair[0], reverse=True)
    return {record.backup_id for _key, record in representatives[:limit]}


def plan_retention(
    records: list[BackupRecord],
    *,
    keep_daily: int,
    keep_weekly: int,
    keep_monthly: int,
) -> set[str]:
    """返回应保留的自动备份编号集合（日／周／月三层取并集，边界使用 UTC）。"""
    automatic = [
        record
        for record in records
        if record.backup_type == BACKUP_TYPE_AUTOMATIC and record.success
    ]
    kept: set[str] = set()
    kept |= _newest_per_bucket(
        automatic,
        key_fn=lambda record: record.created_at.date(),
        limit=keep_daily,
    )
    kept |= _newest_per_bucket(
        automatic,
        key_fn=lambda record: record.created_at.isocalendar()[:2],
        limit=keep_weekly,
    )
    kept |= _newest_per_bucket(
        automatic,
        key_fn=lambda record: (record.created_at.year, record.created_at.month),
        limit=keep_monthly,
    )
    return kept


def package_path_for(backup_dir: Path, backup_id: str) -> Path:
    return backup_dir / f"{backup_id}{PACKAGE_SUFFIX}"


class BackupIndex:
    """整库备份索引的受控存储。"""

    def __init__(self, backup_dir: Path) -> None:
        self.backup_dir = backup_dir
        self.path = backup_dir / INDEX_FILENAME

    def _prepare_directory(self) -> None:
        try:
            if self.backup_dir.is_symlink():
                raise OSError("unsafe backup directory")
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.backup_dir, 0o700)
        except OSError:
            raise BackupIndexCorruptedError("备份目录不可用") from None

    def load(self) -> list[BackupRecord]:
        self._prepare_directory()
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupIndexCorruptedError("备份索引已损坏，无法读取") from exc
        if type(payload) is not dict or set(payload) != INDEX_KEYS:
            raise BackupIndexCorruptedError("备份索引结构不正确")
        version = payload["version"]
        if version not in {1, INDEX_VERSION}:
            raise BackupIndexCorruptedError("备份索引版本不受支持")
        entries = payload["entries"]
        if type(entries) is not list:
            raise BackupIndexCorruptedError("备份索引条目不正确")
        return [_parse_record(entry, version=version) for entry in entries]

    def save(self, records: list[BackupRecord]) -> None:
        self._prepare_directory()
        records = sorted(records, key=_backup_sort_key)
        payload = {
            "version": INDEX_VERSION,
            "entries": [_serialize_record(record) for record in records],
        }
        part: Path | None = None
        try:
            fd, raw_path = tempfile.mkstemp(prefix=".index-", suffix=".part", dir=self.backup_dir)
            part = Path(raw_path)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            os.chmod(part, 0o600)
            os.replace(part, self.path)
        except OSError:
            try:
                if part is not None:
                    part.unlink(missing_ok=True)
            except OSError:
                pass
            raise BackupIndexCorruptedError("无法写入备份索引") from None

    def safe_delete_package(self, backup_id: str) -> None:
        """删除受控目录内的备份包；删除前再次校验路径不越界、不是符号链接。"""
        if _BACKUP_ID_PATTERN.fullmatch(backup_id) is None:
            raise BackupIndexCorruptedError("备份编号不正确，已拒绝删除")
        package = package_path_for(self.backup_dir, backup_id)
        resolved_dir = self.backup_dir.resolve()
        resolved_package = package.resolve()
        if resolved_package.parent != resolved_dir:
            raise BackupIndexCorruptedError("备份路径越界，已拒绝删除")
        if package.is_symlink():
            raise BackupIndexCorruptedError("备份文件是符号链接，已拒绝删除")
        try:
            package.unlink(missing_ok=True)
        except OSError:
            raise BackupIndexCorruptedError("无法删除备份文件") from None


__all__ = [
    "INDEX_FILENAME",
    "INDEX_VERSION",
    "OFFSITE_COPIED",
    "OFFSITE_FAILED",
    "OFFSITE_NOT_CONFIGURED",
    "BackupIndex",
    "BackupIndexCorruptedError",
    "BackupRecord",
    "package_path_for",
    "plan_retention",
]
