# ruff: noqa: RUF002
"""恢复前个人安全备份的受控文件存储。

在个人账本替换恢复前，服务会先用 :func:`~app.services.personal_backup.build_personal_backup`
生成当前账本的 ``.giftledger`` 字节，再通过本模块保存到受控目录，供所属用户在有效期内重复下载，
用于排障或回退。与导入预检临时文件不同，安全备份**不是一次性领取**，因此在有效期内可以反复读取。

安全边界：

- 磁盘文件名只由服务用 ``secrets.token_urlsafe`` 生成，绝不使用上传文件名；
- 元数据（``<token>.json``）记录 ``token``、``user_id``、创建时间、过期时间、字节长度和 SHA-256；
- 内容先写入 ``.part`` 临时文件，再原子改名为最终 ``.giftledger`` 文件；任何异常都会清理半成品；
- 读取时校验 ``token`` 归属、过期时间、字节长度和 SHA-256；其他用户、无效或不存在 ``token`` 返回
  同一个模糊错误，避免泄漏归属；
- 元数据持久化在磁盘，进程重启后仍可读取，不只保存在进程内存；
- 目录由调用者注入，不在代码中硬编码生产路径，也不写入 Git、日志或公共目录；
- 所有错误转换为稳定中文业务错误，不返回绝对路径、SQL 或堆栈。
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_METADATA_KEYS = {
    "version",
    "token",
    "user_id",
    "created_at",
    "expires_at",
    "byte_length",
    "sha256",
}

SAFETY_BACKUP_SUFFIX = ".giftledger"
DEFAULT_TTL_HOURS = 24
ORPHAN_GRACE_PERIOD = timedelta(hours=1)

ERROR_INVALID = "SAFETY_BACKUP_INVALID"
ERROR_EXPIRED = "SAFETY_BACKUP_EXPIRED"
ERROR_CORRUPTED = "SAFETY_BACKUP_CORRUPTED"
ERROR_WRITE_FAILED = "SAFETY_BACKUP_WRITE_FAILED"


class PersonalSafetyBackupStoreError(ValueError):
    """安全备份存储的稳定业务错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SafetyBackupDownload:
    """读取到的安全备份：token、字节与元数据。"""

    token: str
    user_id: int
    content: bytes
    created_at: datetime
    expires_at: datetime
    sha256: str


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _chmod_private(path: Path) -> None:
    """把含个人数据的文件限制为仅属主可读写；失败时不得继续保存。"""
    os.chmod(path, 0o600)


def _is_stale(path: Path, *, current: datetime) -> bool:
    """只清理超过宽限期的孤儿，避免误删另一进程正在完成的原子写。"""
    try:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return False
    return modified_at <= current - ORPHAN_GRACE_PERIOD


class PersonalSafetyBackupStore:
    """恢复前安全备份的受控文件存储，默认保留 24 小时。"""

    def __init__(self, directory: Path, *, ttl_hours: int = DEFAULT_TTL_HOURS) -> None:
        self.directory = directory
        self.ttl = timedelta(hours=ttl_hours)

    def _prepare_directory(self) -> None:
        try:
            if self.directory.is_symlink():
                raise OSError("unsafe storage directory")
            self.directory.mkdir(parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
        except OSError:
            raise PersonalSafetyBackupStoreError(
                ERROR_WRITE_FAILED, "安全备份存储目录不可用"
            ) from None

    def _paths(self, token: str) -> tuple[Path, Path, Path, Path]:
        if not _TOKEN_PATTERN.fullmatch(token):
            raise PersonalSafetyBackupStoreError(ERROR_INVALID, "安全备份凭证无效")
        return (
            self.directory / f"{token}{SAFETY_BACKUP_SUFFIX}",
            self.directory / f"{token}.json",
            self.directory / f"{token}{SAFETY_BACKUP_SUFFIX}.part",
            self.directory / f"{token}.json.part",
        )

    def cleanup_expired(self, *, now: datetime | None = None) -> None:
        """清理过期备份对；同时移除因进程中断遗留的孤儿内容文件。"""
        self._prepare_directory()
        current = now or datetime.now(UTC)
        for metadata_path in self.directory.glob("*.json"):
            content_path = self.directory / f"{metadata_path.stem}{SAFETY_BACKUP_SUFFIX}"
            try:
                payload: dict[str, Any] = json.loads(metadata_path.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(payload["expires_at"])
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                expires_at = current - timedelta(seconds=1)
            if expires_at <= current:
                _unlink_quietly(metadata_path)
                _unlink_quietly(content_path)
        for content_path in self.directory.glob(f"*{SAFETY_BACKUP_SUFFIX}"):
            if not self.directory.joinpath(f"{content_path.stem}.json").exists() and _is_stale(
                content_path, current=current
            ):
                _unlink_quietly(content_path)
        for part_path in self.directory.glob("*.part"):
            if _is_stale(part_path, current=current):
                _unlink_quietly(part_path)

    def save(
        self,
        *,
        user_id: int,
        content: bytes,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        """保存当前账本安全备份，返回 ``(token, expires_at)``。"""
        current = now or datetime.now(UTC)
        self._prepare_directory()
        self.cleanup_expired(now=current)
        expires_at = current + self.ttl
        payload = {
            "version": 1,
            "token": None,
            "user_id": user_id,
            "created_at": current.isoformat(),
            "expires_at": expires_at.isoformat(),
            "byte_length": len(content),
            "sha256": sha256(content).hexdigest(),
        }
        for _attempt in range(5):
            token = secrets.token_urlsafe(32)
            content_path, metadata_path, content_part, metadata_part = self._paths(token)
            if content_path.exists() or metadata_path.exists():
                continue
            payload["token"] = token
            try:
                with content_part.open("xb") as content_file:
                    content_file.write(content)
                _chmod_private(content_part)
            except FileExistsError:
                continue
            except OSError:
                _unlink_quietly(content_part)
                raise PersonalSafetyBackupStoreError(
                    ERROR_WRITE_FAILED, "无法保存恢复前安全备份"
                ) from None
            try:
                os.replace(content_part, content_path)
            except OSError:
                _unlink_quietly(content_part)
                raise PersonalSafetyBackupStoreError(
                    ERROR_WRITE_FAILED, "无法保存恢复前安全备份"
                ) from None
            try:
                with metadata_part.open("x", encoding="utf-8") as metadata_file:
                    json.dump(payload, metadata_file, ensure_ascii=True, separators=(",", ":"))
                _chmod_private(metadata_part)
            except FileExistsError:
                _unlink_quietly(content_path)
                continue
            except OSError:
                _unlink_quietly(content_path)
                _unlink_quietly(metadata_part)
                raise PersonalSafetyBackupStoreError(
                    ERROR_WRITE_FAILED, "无法保存恢复前安全备份"
                ) from None
            try:
                os.replace(metadata_part, metadata_path)
            except OSError:
                _unlink_quietly(content_path)
                _unlink_quietly(metadata_part)
                raise PersonalSafetyBackupStoreError(
                    ERROR_WRITE_FAILED, "无法保存恢复前安全备份"
                ) from None
            return token, expires_at
        raise PersonalSafetyBackupStoreError(ERROR_WRITE_FAILED, "无法保存恢复前安全备份")

    def load(
        self,
        token: str,
        *,
        user_id: int,
        now: datetime | None = None,
    ) -> SafetyBackupDownload:
        """读取安全备份。校验归属、过期、长度与摘要；允许有效期内重复下载。"""
        current = now or datetime.now(UTC)
        self._prepare_directory()
        content_path, metadata_path, _content_part, _metadata_part = self._paths(token)
        try:
            payload: Any = json.loads(metadata_path.read_text(encoding="utf-8"))
            if type(payload) is not dict or set(payload) != _METADATA_KEYS:
                raise ValueError("metadata mismatch")
            if payload["version"] != 1 or payload["token"] != token:
                raise ValueError("metadata mismatch")
            if type(payload["user_id"]) is not int or payload["user_id"] <= 0:
                raise ValueError("metadata mismatch")
            if type(payload["byte_length"]) is not int or payload["byte_length"] < 0:
                raise ValueError("metadata mismatch")
            if (
                type(payload["sha256"]) is not str
                or _SHA256_PATTERN.fullmatch(payload["sha256"]) is None
            ):
                raise ValueError("metadata mismatch")
            stored_user_id = payload["user_id"]
            expires_at = datetime.fromisoformat(payload["expires_at"])
            created_at = datetime.fromisoformat(payload["created_at"])
            if (
                expires_at.tzinfo is None
                or expires_at.utcoffset() != timedelta(0)
                or created_at.tzinfo is None
                or created_at.utcoffset() != timedelta(0)
                or created_at > expires_at
            ):
                raise ValueError("metadata timestamp mismatch")
            byte_length = payload["byte_length"]
            stored_sha256 = payload["sha256"]
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise PersonalSafetyBackupStoreError(
                ERROR_INVALID, "安全备份不存在、已过期或无法读取"
            ) from None
        if stored_user_id != user_id:
            raise PersonalSafetyBackupStoreError(ERROR_INVALID, "安全备份不存在、已过期或无法读取")
        if expires_at <= current:
            _unlink_quietly(content_path)
            _unlink_quietly(metadata_path)
            raise PersonalSafetyBackupStoreError(ERROR_EXPIRED, "恢复前安全备份已过期")
        try:
            content = content_path.read_bytes()
        except OSError:
            raise PersonalSafetyBackupStoreError(
                ERROR_INVALID, "安全备份不存在、已过期或无法读取"
            ) from None
        if len(content) != byte_length or sha256(content).hexdigest() != stored_sha256:
            raise PersonalSafetyBackupStoreError(ERROR_CORRUPTED, "恢复前安全备份文件已损坏")
        return SafetyBackupDownload(
            token=token,
            user_id=user_id,
            content=content,
            created_at=created_at,
            expires_at=expires_at,
            sha256=stored_sha256,
        )


__all__ = [
    "DEFAULT_TTL_HOURS",
    "ERROR_CORRUPTED",
    "ERROR_EXPIRED",
    "ERROR_INVALID",
    "ERROR_WRITE_FAILED",
    "SAFETY_BACKUP_SUFFIX",
    "PersonalSafetyBackupStore",
    "PersonalSafetyBackupStoreError",
    "SafetyBackupDownload",
]
