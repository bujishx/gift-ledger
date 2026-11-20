"""Short-lived sessions for the owner of one configured maintenance site.

Use a pre-provisioned, private directory outside the business database, ledger
files and restored backups. The API process owns this directory. Host-issued
one-time codes are stored as digests; the optional owner password uses the
existing application password hash and policy. No DSM credential is needed.

Pass only the bearer value from the Authorization header. The request origin
is checked against trusted site configuration, independently of URL routing.
"""

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from app.core.security import (
    PasswordPolicyError,
    hash_password,
    validate_new_password,
    verify_password,
    verify_unknown_user_password,
)

SESSION_SECONDS = 30 * 60
ATTEMPT_SECONDS = 5 * 60
MAX_ATTEMPTS = 5
MAX_SESSIONS = 16
APPLICATION_ID = 0x474C5353
DATABASE = "sessions.sqlite3"
TOKEN = re.compile(r"gls1_[a-f0-9]{64}")
CODE_SECONDS = 10 * 60
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_GROUP = f"[{CODE_ALPHABET}]{{4}}"
CODE = re.compile(rf"{CODE_GROUP}(?:[- ]?{CODE_GROUP}){{3}}", re.ASCII | re.IGNORECASE)


class SessionDenied(Exception):
    """The origin, credential or bearer does not authorize this request."""


class SessionRateLimited(Exception):
    """The site has exhausted this kind of failed attempt in the current window."""


class SessionStoreError(Exception):
    """The site's trusted configuration or private session storage is unavailable."""


def _origin(value: str) -> bool:
    try:
        parts = urlsplit(value)
        port = parts.port
        host = parts.hostname or ""
        if not isinstance(value, str) or not host or len(value) > 256 or port == 0:
            return False
        if ":" in host:
            if "%" in host:
                return False
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        elif re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", host.rsplit(".", 1)[-1]):
            # Browsers serialize numeric hosts as four decimal octets.
            host = str(ipaddress.IPv4Address(host))
        elif not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ):
            return False
        suffix = "" if port in (None, 443) else f":{port}"
        return value == f"https://{host}{suffix}"
    except (AttributeError, TypeError, ValueError):
        return False


def _private(path: Path, *, directory: bool) -> tuple[int, int]:
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (
        not kind(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != mode
        or (not directory and info.st_nlink != 1)
    ):
        raise SessionStoreError("maintenance_session_permissions_invalid")
    return info.st_dev, info.st_ino


class OwnerSessions:
    """Persistent bearer digests scoped to an exact project, instance and HTTPS origin."""

    def __init__(
        self,
        directory: Path,
        *,
        project: str,
        instance_id: str,
        origin: str,
        clock: Callable[[], float] = time.time,
    ):
        try:
            valid_uuid = str(UUID(instance_id)) == instance_id
        except (AttributeError, TypeError, ValueError):
            valid_uuid = False
        if not (
            isinstance(project, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project)
            and valid_uuid
            and _origin(origin)
        ):
            raise SessionStoreError("maintenance_session_binding_invalid")
        self.binding = (project, instance_id, origin)
        self.origin = origin
        self.clock = clock
        self.directory = Path(directory)
        self.path = self.directory / DATABASE
        try:
            if not self.directory.is_absolute() or self.directory.resolve() != self.directory:
                raise SessionStoreError("maintenance_session_directory_invalid")
            self.directory_id = _private(self.directory, directory=True)
            # Open without following a symlink, including during concurrent first use.
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(self.path, flags, 0o600)
            os.close(fd)
            self.file_id = _private(self.path, directory=False)
        except OSError:
            raise SessionStoreError("maintenance_session_store_unavailable") from None
        with self._transaction(write=True, initialize=True):
            pass

    @contextmanager
    def _transaction(
        self, *, write: bool = False, initialize: bool = False
    ) -> Iterator[sqlite3.Connection]:
        try:
            if (
                _private(self.directory, directory=True) != self.directory_id
                or _private(self.path, directory=False) != self.file_id
            ):
                raise SessionStoreError("maintenance_session_store_replaced")
            # The API is the only writer of this dedicated mode-0700 directory.
            # A rollback journal can legitimately disappear as another connection commits.
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    _private(self.path.with_name(DATABASE + suffix), directory=False)
                except FileNotFoundError:
                    continue
                if suffix != "-journal":
                    raise SessionStoreError("maintenance_session_store_unexpected_journal")
            uri = self.path.as_uri() + "?mode=rw"
            with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)) as db:
                db.execute("PRAGMA trusted_schema=OFF")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                if initialize:
                    self._initialize(db)
                bound = db.execute(
                    "SELECT project, instance_id, origin FROM site WHERE id = 1"
                ).fetchone()
                if bound != self.binding:
                    raise SessionStoreError("maintenance_session_binding_mismatch")
                yield db
                db.commit()
        except (sqlite3.Error, OSError):
            raise SessionStoreError("maintenance_session_store_unavailable") from None

    def _initialize(self, db: sqlite3.Connection) -> None:
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if app_id == APPLICATION_ID and version == 2:
            return
        if app_id == APPLICATION_ID and version == 1:
            # Early session-only databases had no site owner credential.
            bound = db.execute(
                "SELECT project, instance_id, origin FROM site WHERE id=1"
            ).fetchone()
            if bound != self.binding:
                raise SessionStoreError("maintenance_session_binding_mismatch")
            self._clear_sessions(db)
            self._create_credentials(db)
            return
        if app_id or version or db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            raise SessionStoreError("maintenance_session_store_unknown")
        db.execute(
            "CREATE TABLE site(id INTEGER PRIMARY KEY CHECK(id=1), project TEXT NOT NULL, "
            "instance_id TEXT NOT NULL, origin TEXT NOT NULL, generation INTEGER NOT NULL)"
        )
        db.execute(
            "CREATE TABLE sessions(digest BLOB PRIMARY KEY, generation INTEGER NOT NULL, "
            "issued INTEGER NOT NULL, expires INTEGER NOT NULL)"
        )
        db.execute(
            "CREATE TABLE attempts(id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "started INTEGER NOT NULL)"
        )
        db.execute("INSERT INTO site VALUES (1, ?, ?, ?, 1)", self.binding)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        self._create_credentials(db)

    @staticmethod
    def _create_credentials(db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE owner(id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT, "
            "code_digest BLOB, code_issued INTEGER, code_expires INTEGER)"
        )
        db.execute("INSERT INTO owner(id) VALUES(1)")
        db.execute("PRAGMA user_version=2")

    def _check_origin(self, origin: str | None, *, mutation: bool) -> None:
        if (mutation or origin is not None) and origin != self.origin:
            raise SessionDenied("maintenance_origin_refused")

    def record_business_login(self, user_id: int, username: str) -> None:
        # Per-instance owner state stays outside restored business records. Keep only
        # the most recent successful sign-in, with no session identifier or client IP.
        with self._transaction(write=True) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS business_logins("
                "user_id INTEGER PRIMARY KEY, username TEXT NOT NULL, at INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT INTO business_logins(user_id,username,at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, at=excluded.at",
                (user_id, username, int(self.clock())),
            )

    def business_logins(self, user_ids: list[int]) -> dict[tuple[int, str], int]:
        if not user_ids:
            return {}
        with self._transaction() as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_logins'"
            ).fetchone():
                return {}
            slots = ",".join("?" for _ in user_ids)
            rows = db.execute(
                f"SELECT user_id,username,at FROM business_logins WHERE user_id IN ({slots})",
                user_ids,
            )
            return {(uid, name): timestamp for uid, name, timestamp in rows}

    def _reserve(self, db: sqlite3.Connection, kind: str, now: int) -> str:
        db.execute("DELETE FROM attempts WHERE started <= ?", (now - ATTEMPT_SECONDS,))
        count = db.execute("SELECT count(*) FROM attempts WHERE kind=?", (kind,)).fetchone()[0]
        if count >= MAX_ATTEMPTS:
            raise SessionRateLimited("maintenance_attempts_limited")
        ticket = secrets.token_hex(16)
        db.execute("INSERT INTO attempts VALUES (?, ?, ?)", (ticket, kind, now))
        return ticket

    def issue_login_code(self) -> str:
        """Local server operation only. Rotate the pending code, returning its sole clear copy."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(16))
        with self._transaction(write=True) as db:
            now = int(self.clock())
            db.execute(
                "UPDATE owner SET code_digest=?, code_issued=?, code_expires=? WHERE id=1",
                (hashlib.sha256(code.encode("ascii")).digest(), now, now + CODE_SECONDS),
            )
            # Physical server access allows immediate recovery after mistyped codes.
            db.execute("DELETE FROM attempts WHERE kind='code'")
        return "-".join(code[i : i + 4] for i in range(0, 16, 4))

    def _begin_attempt(self, db: sqlite3.Connection, kind: str) -> tuple[str, int]:
        ticket = self._reserve(db, kind, int(self.clock()))
        generation = db.execute("SELECT generation FROM site WHERE id=1").fetchone()[0]
        return ticket, generation

    def _finish_attempt(self, db: sqlite3.Connection, ticket: str, generation: int) -> int:
        now = int(self.clock())
        result = db.execute(
            "DELETE FROM attempts WHERE id=? AND started>? AND started<=? "
            "AND EXISTS (SELECT 1 FROM site WHERE id=1 AND generation=?)",
            (ticket, now - ATTEMPT_SECONDS, now, generation),
        )
        if result.rowcount != 1:
            raise SessionDenied("maintenance_login_refused")
        return now

    @staticmethod
    def _issue(db: sqlite3.Connection, now: int) -> str:
        db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
        db.execute(
            "DELETE FROM sessions WHERE digest IN (SELECT digest FROM sessions "
            "ORDER BY issued DESC, rowid DESC LIMIT -1 OFFSET ?)",
            (MAX_SESSIONS - 1,),
        )
        token = "gls1_" + secrets.token_hex(32)
        db.execute(
            "INSERT INTO sessions SELECT ?, generation, ?, ? FROM site WHERE id=1",
            (hashlib.sha256(token.encode("ascii")).digest(), now, now + SESSION_SECONDS),
        )
        return token

    def login_with_password(self, password: str, *, request_origin: str) -> str:
        self._check_origin(request_origin, mutation=True)
        with self._transaction(write=True) as db:
            ticket, generation = self._begin_attempt(db, "password")
            stored = db.execute("SELECT password_hash FROM owner WHERE id=1").fetchone()[0]
        if not isinstance(password, str) or not 0 < len(password) <= 128:
            raise SessionDenied("maintenance_login_refused")
        # Keep the expensive password check outside the write transaction.
        if stored is None:
            verify_unknown_user_password(password)
            raise SessionDenied("maintenance_login_refused")
        if not verify_password(password, stored):
            raise SessionDenied("maintenance_login_refused")
        with self._transaction(write=True) as db:
            now = self._finish_attempt(db, ticket, generation)
            return self._issue(db, now)

    def login_with_code(self, code: str, *, request_origin: str) -> str:
        return self._with_code(code, request_origin, change_password=False)

    def set_password_with_code(self, code: str, new_password: str, *, request_origin: str) -> str:
        """Set or replace the optional owner password; a bearer alone cannot change it."""
        if not isinstance(new_password, str):
            self._check_origin(request_origin, mutation=True)
            raise PasswordPolicyError("PASSWORD_INVALID", "请输入新的站主密码")
        return self._with_code(code, request_origin, change_password=True, password=new_password)

    def remove_password_with_code(self, code: str, *, request_origin: str) -> str:
        """Resume code-only operation and invalidate sessions proving an older credential."""
        return self._with_code(code, request_origin, change_password=True)

    def _with_code(
        self, code: str, origin: str, *, change_password: bool, password: str | None = None
    ) -> str:
        self._check_origin(origin, mutation=True)
        if password is not None:
            validate_new_password(password)
        with self._transaction(write=True) as db:
            ticket, generation = self._begin_attempt(db, "code")
            expected, issued, expires = db.execute(
                "SELECT code_digest, code_issued, code_expires FROM owner WHERE id=1"
            ).fetchone()
        if not isinstance(code, str) or len(code) > 19 or not CODE.fullmatch(code):
            raise SessionDenied("maintenance_login_refused")
        raw = code.replace("-", "").replace(" ", "").upper().encode("ascii")
        digest = hashlib.sha256(raw).digest()
        if expected is None or not issued <= int(self.clock()) < expires:
            raise SessionDenied("maintenance_login_refused")
        if not hmac.compare_digest(digest, expected):
            raise SessionDenied("maintenance_login_refused")
        encoded = None if password is None else hash_password(password)
        with self._transaction(write=True) as db:
            # A new code, another successful request or a reset can win while Argon2 runs.
            # Consuming this code, updating the password and issuing the session are one commit.
            now = self._finish_attempt(db, ticket, generation)
            used = db.execute(
                "UPDATE owner SET code_digest=NULL, code_issued=NULL, code_expires=NULL "
                "WHERE id=1 AND code_digest=? AND code_issued<=? AND code_expires>?",
                (digest, now, now),
            )
            if used.rowcount != 1:
                raise SessionDenied("maintenance_login_refused")
            if change_password:
                db.execute("UPDATE owner SET password_hash=? WHERE id=1", (encoded,))
                self._clear_sessions(db)
            return self._issue(db, now)

    def _lookup(self, db: sqlite3.Connection, token: str, now: int) -> bytes | None:
        if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
            return None
        digest = hashlib.sha256(token.encode("ascii")).digest()
        found = db.execute(
            "SELECT 1 FROM sessions JOIN site ON site.id=1 AND site.generation=sessions.generation "
            "WHERE digest=? AND issued<=? AND expires>?",
            (digest, now, now),
        ).fetchone()
        return digest if found else None

    def require(
        self, token: str, *, request_origin: str | None = None, mutation: bool = False
    ) -> None:
        """Validate a bearer; unsafe HTTP methods must pass mutation=True and their exact Origin."""
        self._check_origin(request_origin, mutation=mutation)
        now = int(self.clock())
        with self._transaction() as db:
            if self._lookup(db, token, now) is not None:
                return
        # Valid sessions are independent of bad tokens or failed credential guesses.
        with self._transaction(write=True) as db:
            self._reserve(db, "bearer", now)
        raise SessionDenied("maintenance_session_refused")

    def logout(self, token: str, *, request_origin: str) -> None:
        """Revoke only this browser session."""
        self._check_origin(request_origin, mutation=True)
        with self._transaction(write=True) as db:
            now = int(self.clock())
            digest = self._lookup(db, token, now)
            if digest is not None:
                db.execute("DELETE FROM sessions WHERE digest=?", (digest,))
                return
            self._reserve(db, "bearer", now)
        raise SessionDenied("maintenance_session_refused")

    @staticmethod
    def _clear_sessions(db: sqlite3.Connection) -> None:
        db.execute("UPDATE site SET generation=generation+1 WHERE id=1")
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM attempts")

    def revoke_all(self) -> None:
        """Authorized server operation. Invalidate every session and pending login code."""
        with self._transaction(write=True) as db:
            self._clear_sessions(db)
            db.execute("UPDATE owner SET code_digest=NULL, code_issued=NULL, code_expires=NULL")
