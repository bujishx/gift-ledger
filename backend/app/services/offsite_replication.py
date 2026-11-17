# ruff: noqa: RUF002, RUF003
"""整库备份的通用「本机加密后复制到已挂载异地目录」适配器。

本模块不绑定 NAS／云厂商协议，只面向一个已经挂载好的本地目录（NFS、SMB、USB 盘等），
使用成熟的 ``age`` CLI 做公钥加密。接收者公钥可配置；解密私钥只由本机恢复命令从 Git 外
路径读取，绝不进入本模块、Git、备份 manifest、状态 JSON、命令参数或日志。

安全边界：

- 加密、复制或校验失败绝不把未完成文件冒充成功副本；写入先落 ``.part`` 再原子改名，并在
  复制后核对大小与 SHA-256；
- 未配置异地目录或 age 接收者时，主备份仍可成功，但状态明确显示「异地副本未配置」；
- 不把接收者私钥、数据库密码或任何密钥写入 Git、备份 manifest、状态 JSON、命令参数和日志。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.services.backup_index import (
    OFFSITE_COPIED,
    OFFSITE_FAILED,
    OFFSITE_NOT_CONFIGURED,
)
from app.services.database_backup import PACKAGE_SUFFIX, DatabaseBackupError, sha256_file

AGE_SUFFIX = ".age"
AGE_ENCRYPTED_SUFFIX = f"{PACKAGE_SUFFIX}{AGE_SUFFIX}"
_BACKUP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@dataclass(frozen=True, slots=True)
class OffsiteResult:
    status: str
    sha256: str | None


class OffsiteReplicator:
    """把 ``.gladminbackup`` 包加密后复制到异地目录。"""

    def __init__(
        self,
        *,
        offsite_dir: Path | None,
        recipients: tuple[str, ...],
        age_path: str,
        timeout: int,
    ) -> None:
        self.offsite_dir = offsite_dir
        self.recipients = recipients
        self.age_path = age_path
        self.timeout = timeout

    def configured(self) -> bool:
        return self.offsite_dir is not None and bool(self.recipients)

    def replicate(self, *, backup_id: str, package_path: Path, work_dir: Path) -> OffsiteResult:
        """加密并复制；返回复制状态与加密副本 SHA-256。"""
        if not self.configured():
            return OffsiteResult(status=OFFSITE_NOT_CONFIGURED, sha256=None)

        encrypted_local = work_dir / f"{backup_id}{AGE_ENCRYPTED_SUFFIX}"
        final_offsite = self.offsite_dir / f"{backup_id}{AGE_ENCRYPTED_SUFFIX}"
        offsite_part = self.offsite_dir / f"{backup_id}{AGE_ENCRYPTED_SUFFIX}.part"
        try:
            self._encrypt(package_path, encrypted_local)

            digest, size = sha256_file(encrypted_local)
            self._copy_to_offsite(encrypted_local, offsite_part, final_offsite)

            # 复制后核对大小与校验值，防止挂载目录上的部分写入或损坏。
            offsite_digest, offsite_size = sha256_file(final_offsite)
            if offsite_digest != digest or offsite_size != size:
                raise DatabaseBackupError("backup_offsite_verify_failed", "异地副本校验失败")
        except (DatabaseBackupError, OSError, subprocess.SubprocessError):
            self._unlink_quietly(encrypted_local)
            self._unlink_quietly(offsite_part)
            return OffsiteResult(status=OFFSITE_FAILED, sha256=None)

        self._unlink_quietly(encrypted_local)
        return OffsiteResult(status=OFFSITE_COPIED, sha256=digest)

    def _encrypt(self, source: Path, target: Path) -> None:
        argv = [self.age_path]
        for recipient in self.recipients:
            argv.extend(["-r", recipient])
        argv.extend(["-o", str(target), str(source)])
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DatabaseBackupError("backup_age_unavailable", "找不到 age 加密工具") from exc
        except subprocess.TimeoutExpired as exc:
            raise DatabaseBackupError("backup_age_timeout", "异地加密超时") from exc
        if result.returncode != 0:
            raise DatabaseBackupError("backup_age_failed", "异地加密失败")
        os.chmod(target, 0o600)

    def _copy_to_offsite(self, source: Path, part: Path, final: Path) -> None:
        try:
            if self.offsite_dir.is_symlink():
                raise OSError("unsafe offsite directory")
            self.offsite_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.offsite_dir, 0o700)
            if part.exists():
                part.unlink()
            with part.open("xb") as target:
                with source.open("rb") as stream:
                    shutil.copyfileobj(stream, target, length=1024 * 1024)
            os.chmod(part, 0o600)
            os.replace(part, final)
        except OSError:
            self._unlink_quietly(part)
            raise

    def delete(self, backup_id: str) -> None:
        """清理已过保留期的受控异地副本；未配置异地目录时不做猜测性删除。"""

        if not self.configured():
            return
        if _BACKUP_ID_PATTERN.fullmatch(backup_id) is None:
            raise DatabaseBackupError("backup_offsite_path_unsafe", "异地备份编号不正确")
        if self.offsite_dir.is_symlink():
            raise DatabaseBackupError("backup_offsite_path_unsafe", "异地备份目录不可用")
        final = self.offsite_dir / f"{backup_id}{AGE_ENCRYPTED_SUFFIX}"
        try:
            resolved_dir = self.offsite_dir.resolve()
            if final.resolve().parent != resolved_dir or final.is_symlink():
                raise DatabaseBackupError("backup_offsite_path_unsafe", "异地备份路径越界")
            final.unlink(missing_ok=True)
        except DatabaseBackupError:
            raise
        except OSError:
            raise DatabaseBackupError(
                "backup_offsite_delete_failed", "无法清理过期异地备份"
            ) from None

    def _unlink_quietly(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = ["AGE_ENCRYPTED_SUFFIX", "OffsiteReplicator", "OffsiteResult"]
