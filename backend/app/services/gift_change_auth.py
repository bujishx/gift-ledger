# ruff: noqa: RUF002
"""流水修改／删除的会话级密码再验证状态。

业务规则（API_CONTRACT 与 D117）：
- 每次登录后的首次修改／删除流水前必须验证密码；
- 验证成功后在同一登录会话内固定有效 60 分钟，到期需再次验证；
- 修改／删除操作不顺延有效期；
- 退出登录或换会话（新登录、并发会话）后立即失效。

该状态只保存在当前进程内存中，按登录令牌的 ``jti``（每次登录随机生成、
全局唯一）作为会话标识，不新增数据库表、不改变数据口径。
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock

GIFT_CHANGE_REAUTH_TTL = timedelta(minutes=60)


class GiftChangeReauthStore:
    """按会话记录密码再验证时刻的内存存储。"""

    def __init__(
        self,
        ttl: timedelta = GIFT_CHANGE_REAUTH_TTL,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ttl = ttl
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._verified: dict[str, datetime] = {}
        self._lock = Lock()

    def mark_verified(self, session_id: str) -> None:
        if not session_id:
            raise ValueError("会话标识不能为空")
        with self._lock:
            self._verified[session_id] = self._now()

    def is_verified(self, session_id: str) -> bool:
        if not session_id:
            return False
        with self._lock:
            verified_at = self._verified.get(session_id)
            if verified_at is None:
                return False
            if self._now() - verified_at >= self._ttl:
                self._verified.pop(session_id, None)
                return False
            return True

    def revoke(self, session_id: str) -> None:
        with self._lock:
            self._verified.pop(session_id, None)

    def clear(self) -> None:
        with self._lock:
            self._verified.clear()
