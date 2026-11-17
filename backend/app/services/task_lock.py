# ruff: noqa: RUF002, RUF003
"""整库备份与恢复任务的跨进程互斥锁。

同一时刻只允许一个整库备份／恢复任务运行。本模块提供一个跨平台（Windows／Linux）的
尽力而为文件锁：以 ``O_CREAT | O_EXCL`` 创建锁文件表示加锁，锁内记录启动时间与 PID，
超过超时时间后视为陈旧锁并允许抢占。它不是强一致分布式锁，但足以阻止本机维护命令与
网页触发的任务相互重入，避免两个 mysqldump／restore 同时写同一目录。

安全边界：锁文件只放在调用者注入的受控目录内，文件名固定，不含用户输入。
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

LOCK_FILENAME = ".task.lock"
DEFAULT_STALE_SECONDS = 4 * 3600


class TaskLockBusyError(RuntimeError):
    """已有备份／恢复任务正在运行，拒绝并发执行。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class _LockMetadata:
    created_at: float
    pid: int
    owner_token: str


class TaskLock:
    """以目录内独占文件实现的互斥锁。"""

    def __init__(self, directory: Path, *, stale_seconds: int = DEFAULT_STALE_SECONDS) -> None:
        self.path = directory / LOCK_FILENAME
        self.stale_seconds = stale_seconds
        self.owner_token = secrets.token_hex(16)

    def _read_metadata(self) -> _LockMetadata | None:
        try:
            created_at, pid, owner_token = self.path.read_text(encoding="ascii").strip().split(" ")
            if len(owner_token) != 32:
                return None
            return _LockMetadata(
                created_at=float(created_at),
                pid=int(pid),
                owner_token=owner_token,
            )
        except (OSError, ValueError):
            return None

    def _is_stale(self) -> bool:
        metadata = self._read_metadata()
        if metadata is None:
            try:
                return time.time() - self.path.stat().st_mtime > self.stale_seconds
            except OSError:
                return False
        return time.time() - metadata.created_at > self.stale_seconds

    def acquire(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
        except OSError:
            # 目录权限调整失败不应阻断加锁，但也不能掩盖真正的占用。
            pass
        try:
            with self.path.open("x", encoding="ascii") as lock_file:
                lock_file.write(f"{time.time()} {os.getpid()} {self.owner_token}")
        except FileExistsError:
            if self._is_stale():
                self._break_stale_lock()
                return self.acquire()
            raise TaskLockBusyError("已有备份或恢复任务正在运行，请稍后再试") from None
        except OSError as exc:
            raise TaskLockBusyError("无法创建任务锁，请检查备份目录权限") from exc

    def _break_stale_lock(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise TaskLockBusyError("已有备份或恢复任务正在运行，请稍后再试") from None

    def release(self) -> None:
        metadata = self._read_metadata()
        if metadata is None or metadata.owner_token != self.owner_token:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self) -> TaskLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


__all__ = ["TaskLock", "TaskLockBusyError"]
