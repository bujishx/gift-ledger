# ruff: noqa: RUF002, RUF003
"""个人备份恢复预览的受控文件存储。

恢复预览分两步：用户上传 ``.giftledger`` 后，接口只做只读预检；预检通过才把**原始上传字节**
保存到受控目录并签发一次性确认 token。本模块负责这个一次性 token 的生命周期：

- 磁盘文件名只由服务用 ``secrets.token_urlsafe(32)`` 生成，绝不使用上传文件名；
- 元数据（``<token>.json``）记录 ``version``、``token``、``user_id``、``created_at``、
  ``expires_at``、``byte_length`` 和 ``sha256``，持久化在磁盘，进程重启后仍可读取；
- 内容先写 ``.part`` 临时文件再原子改名为最终 ``.giftledger`` 文件，异常时清理半成品；
- ``claim()`` 用原子改名把元数据移动到 ``.claimed`` 位置，保证并发确认只有一个成功，并返回可在
  ``finally`` 中销毁的对象；无论确认成功、密码错误、状态冲突或未知失败，已领取的 token 都一次性
  消耗，用户输错密码后必须重新上传预检；
- 默认保留 30 分钟；``cleanup_expired()`` 清理过期对、旧 ``.part`` 和旧孤儿文件，同时给并发
  写入留一小时宽限期，避免误删另一进程正在完成的原子写；
- 目录由调用者注入，目录权限设为 ``0700``、文件权限设为 ``0600``，权限失败立即停止；
- 其他用户、不存在、已使用和畸形 token 返回同一个模糊错误；所有错误转为稳定中文业务错误，
  不返回绝对路径、SQL、人员数据或堆栈。
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
    "ledger_revision",
    "byte_length",
    "sha256",
}

PREVIEW_SUFFIX = ".giftledger"
CLAIMED_SUFFIX = ".claimed"
DEFAULT_TTL_MINUTES = 30
ORPHAN_GRACE_PERIOD = timedelta(hours=1)

ERROR_INVALID = "PERSONAL_RESTORE_INVALID"
ERROR_EXPIRED = "PERSONAL_RESTORE_EXPIRED"
ERROR_CORRUPTED = "PERSONAL_RESTORE_CORRUPTED"
ERROR_WRITE_FAILED = "PERSONAL_RESTORE_WRITE_FAILED"


class PersonalRestorePreviewStoreError(ValueError):
    """恢复预览文件存储的稳定业务错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class StoredPersonalRestorePreview:
    """已领取的恢复预览：原始上传字节与受控文件位置。"""

    token: str
    user_id: int
    content: bytes
    expires_at: datetime
    ledger_revision: str
    claimed_path: Path
    content_path: Path

    def discard(self) -> None:
        _discard_stored_files(self.claimed_path, self.content_path)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _discard_stored_files(claimed_path: Path, content_path: Path) -> None:
    # 先删内容，避免元数据先消失导致清理逻辑把内容当孤儿遗留；再删领取标记。
    _unlink_quietly(content_path)
    _unlink_quietly(claimed_path)


def _chmod_private(path: Path) -> None:
    """把含个人数据的文件限制为仅属主可读写；失败时不得继续。"""
    os.chmod(path, 0o600)


def _is_stale(path: Path, *, current: datetime) -> bool:
    try:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return False
    return modified_at <= current - ORPHAN_GRACE_PERIOD


class PersonalRestorePreviewStore:
    """恢复预览一次性 token 的受控文件存储，默认保留 30 分钟。"""

    def __init__(self, directory: Path, *, ttl_minutes: int = DEFAULT_TTL_MINUTES) -> None:
        self.directory = directory
        self.ttl = timedelta(minutes=ttl_minutes)

    def _prepare_directory(self) -> None:
        try:
            if self.directory.is_symlink():
                raise OSError("unsafe storage directory")
            self.directory.mkdir(parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
        except OSError:
            raise PersonalRestorePreviewStoreError(
                ERROR_WRITE_FAILED, "恢复预览存储目录不可用"
            ) from None

    def _paths(self, token: str) -> tuple[Path, Path, Path, Path, Path]:
        if not _TOKEN_PATTERN.fullmatch(token):
            raise PersonalRestorePreviewStoreError(ERROR_INVALID, "恢复预览凭证无效")
        return (
            self.directory / f"{token}.json",
            self.directory / f"{token}{CLAIMED_SUFFIX}",
            self.directory / f"{token}{PREVIEW_SUFFIX}",
            self.directory / f"{token}.json.part",
            self.directory / f"{token}{PREVIEW_SUFFIX}.part",
        )

    def _read_expiry(self, metadata_path: Path, current: datetime) -> datetime:
        try:
            payload: Any = json.loads(metadata_path.read_text(encoding="utf-8"))
            expires_at = datetime.fromisoformat(payload["expires_at"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return current - timedelta(seconds=1)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at

    def cleanup_expired(self, *, now: datetime | None = None) -> None:
        """清理过期对；同时移除超过宽限期的孤儿内容文件和半成品 ``.part``。"""
        self._prepare_directory()
        current = now or datetime.now(UTC)
        for metadata_path in (
            *self.directory.glob("*.json"),
            *self.directory.glob(f"*{CLAIMED_SUFFIX}"),
        ):
            content_path = self.directory / f"{metadata_path.stem}{PREVIEW_SUFFIX}"
            if self._read_expiry(metadata_path, current) <= current:
                _unlink_quietly(metadata_path)
                _unlink_quietly(content_path)
        for content_path in self.directory.glob(f"*{PREVIEW_SUFFIX}"):
            claimed_path = self.directory / f"{content_path.stem}{CLAIMED_SUFFIX}"
            metadata_path = self.directory / f"{content_path.stem}.json"
            if (
                not claimed_path.exists()
                and not metadata_path.exists()
                and _is_stale(content_path, current=current)
            ):
                _unlink_quietly(content_path)
        for part_path in self.directory.glob("*.part"):
            if _is_stale(part_path, current=current):
                _unlink_quietly(part_path)

    def create(
        self,
        *,
        user_id: int,
        content: bytes,
        ledger_revision: str,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        """保存原始上传字节，返回 ``(token, expires_at)``。"""
        current = now or datetime.now(UTC)
        self._prepare_directory()
        self.cleanup_expired(now=current)
        expires_at = current + self.ttl
        payload = {
            "version": 2,
            "ledger_revision": ledger_revision,
            "token": None,
            "user_id": user_id,
            "created_at": current.isoformat(),
            "expires_at": expires_at.isoformat(),
            "byte_length": len(content),
            "sha256": sha256(content).hexdigest(),
        }
        for _attempt in range(5):
            token = secrets.token_urlsafe(32)
            metadata_path, claimed_path, content_path, metadata_part, content_part = self._paths(
                token
            )
            if metadata_path.exists() or claimed_path.exists() or content_path.exists():
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
                raise PersonalRestorePreviewStoreError(
                    ERROR_WRITE_FAILED, "无法保存恢复预览文件"
                ) from None
            try:
                os.replace(content_part, content_path)
            except OSError:
                _unlink_quietly(content_part)
                raise PersonalRestorePreviewStoreError(
                    ERROR_WRITE_FAILED, "无法保存恢复预览文件"
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
                raise PersonalRestorePreviewStoreError(
                    ERROR_WRITE_FAILED, "无法保存恢复预览文件"
                ) from None
            try:
                os.replace(metadata_part, metadata_path)
            except OSError:
                _unlink_quietly(content_path)
                _unlink_quietly(metadata_part)
                raise PersonalRestorePreviewStoreError(
                    ERROR_WRITE_FAILED, "无法保存恢复预览文件"
                ) from None
            return token, expires_at
        raise PersonalRestorePreviewStoreError(ERROR_WRITE_FAILED, "无法保存恢复预览文件")

    def claim(
        self,
        token: str,
        *,
        user_id: int,
        now: datetime | None = None,
    ) -> StoredPersonalRestorePreview:
        """原子领取一次性 token，校验归属、过期、长度与摘要；任何确认结果都只消耗一次。"""
        current = now or datetime.now(UTC)
        self._prepare_directory()
        metadata_path, claimed_path, content_path, _metadata_part, _content_part = self._paths(
            token
        )
        try:
            payload: Any = json.loads(metadata_path.read_text(encoding="utf-8"))
            if type(payload) is not dict or set(payload) != _METADATA_KEYS:
                raise ValueError("metadata mismatch")
            if (
                payload["version"] != 2
                or payload["token"] != token
                or not isinstance(payload["ledger_revision"], str)
                or not _SHA256_PATTERN.fullmatch(payload["ledger_revision"])
            ):
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
            raise PersonalRestorePreviewStoreError(
                ERROR_INVALID, "恢复预览凭证不存在、已过期或已经使用"
            ) from None
        if stored_user_id != user_id:
            raise PersonalRestorePreviewStoreError(
                ERROR_INVALID, "恢复预览凭证不存在、已过期或已经使用"
            )
        if expires_at <= current:
            _unlink_quietly(content_path)
            _unlink_quietly(metadata_path)
            raise PersonalRestorePreviewStoreError(ERROR_EXPIRED, "恢复预览已过期，请重新上传")
        try:
            os.replace(metadata_path, claimed_path)
        except OSError:
            raise PersonalRestorePreviewStoreError(
                ERROR_INVALID, "恢复预览凭证不存在、已过期或已经使用"
            ) from None
        try:
            content = content_path.read_bytes()
        except OSError:
            _unlink_quietly(claimed_path)
            _unlink_quietly(content_path)
            raise PersonalRestorePreviewStoreError(
                ERROR_INVALID, "恢复预览凭证不存在、已过期或已经使用"
            ) from None
        if len(content) != byte_length or sha256(content).hexdigest() != stored_sha256:
            _unlink_quietly(claimed_path)
            _unlink_quietly(content_path)
            raise PersonalRestorePreviewStoreError(ERROR_CORRUPTED, "恢复预览文件已损坏")
        return StoredPersonalRestorePreview(
            token=token,
            user_id=user_id,
            content=content,
            expires_at=expires_at,
            ledger_revision=payload["ledger_revision"],
            claimed_path=claimed_path,
            content_path=content_path,
        )


__all__ = [
    "CLAIMED_SUFFIX",
    "DEFAULT_TTL_MINUTES",
    "ERROR_CORRUPTED",
    "ERROR_EXPIRED",
    "ERROR_INVALID",
    "ERROR_WRITE_FAILED",
    "PREVIEW_SUFFIX",
    "PersonalRestorePreviewStore",
    "PersonalRestorePreviewStoreError",
    "StoredPersonalRestorePreview",
]
