"""Short, single-use grants for bulk account operations; no password is retained."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from threading import Lock

GRANT_TTL = timedelta(minutes=5)
ATTEMPT_WINDOW = timedelta(minutes=15)
MAX_PASSWORD_ATTEMPTS = 5


class SensitiveActionError(ValueError):
    def __init__(self, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class _Grant:
    user_id: int
    session_id: str
    purpose: str
    context: str
    expires_at: datetime


class SensitiveActionStore:
    """Atomic consumption, account-wide attempt limit and session-scoped grants.

    The LAN API currently has one worker. Restarting it invalidates all grants.
    A future multi-worker deployment must replace this store with shared atomic
    storage before increasing the worker count.
    """

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._grants: dict[str, _Grant] = {}
        self._attempts: dict[int, list[datetime]] = {}
        self._lock = Lock()

    def begin_password_attempt(self, user_id: int) -> None:
        with self._lock:
            now = self._now()
            attempts = [
                stamp for stamp in self._attempts.get(user_id, [])
                if stamp + ATTEMPT_WINDOW > now
            ]
            if len(attempts) >= MAX_PASSWORD_ATTEMPTS:
                retry = max(1, int((attempts[0] + ATTEMPT_WINDOW - now).total_seconds()))
                raise SensitiveActionError(
                    "PASSWORD_VERIFICATION_LIMITED", "密码验证次数过多，请稍后重试", retry
                )
            attempts.append(now)
            self._attempts[user_id] = attempts

    def password_succeeded(self, user_id: int) -> None:
        with self._lock:
            self._attempts.pop(user_id, None)

    def issue(
        self, *, user_id: int, session_id: str, purpose: str, context: str = ""
    ) -> tuple[str, datetime]:
        if not session_id:
            raise ValueError("Session identity is required")
        with self._lock:
            now = self._now()
            self._grants = {
                key: grant for key, grant in self._grants.items() if grant.expires_at > now
            }
            token = token_urlsafe(32)
            expires_at = now + GRANT_TTL
            self._grants[sha256(token.encode()).hexdigest()] = _Grant(
                user_id, session_id, purpose, context, expires_at
            )
            return token, expires_at

    def consume(
        self, token: str | None, *, user_id: int, session_id: str,
        purpose: str, context: str = "",
    ) -> None:
        with self._lock:
            key = sha256((token or "").encode()).hexdigest()
            grant = self._grants.get(key)
            if grant is not None and grant.expires_at <= self._now():
                self._grants.pop(key, None)
                grant = None
            if grant is None or (
                grant.user_id, grant.session_id, grant.purpose, grant.context
            ) != (user_id, session_id, purpose, context):
                raise SensitiveActionError(
                    "SENSITIVE_ACTION_REAUTH_REQUIRED",
                    "请重新验证密码后执行本次操作",
                )
            self._grants.pop(key)

    def revoke_session(self, session_id: str) -> None:
        with self._lock:
            self._grants = {
                key: grant for key, grant in self._grants.items()
                if grant.session_id != session_id
            }


def query_export_context(filters) -> str:
    """Canonical validated query binds every filter, scope and paging choice."""
    return sha256(filters.model_dump_json().encode()).hexdigest()


def import_plan_context(token: str, plan_fingerprint: str) -> str:
    return sha256(f"{token}:{plan_fingerprint}".encode()).hexdigest()
