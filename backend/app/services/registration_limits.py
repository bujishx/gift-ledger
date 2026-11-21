"""Bound failed or concurrent credential checks for the single-worker site API."""

import math
import os
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock, local


class AttemptLimitReached(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


class LoginBusy(Exception):
    pass


# CLI jobs, owner actions, registrations and logins share the same expensive-work slot.
# The file lock is taken on the existing app-data directory, including across API workers.
_process_slot = Lock()
_held = local()


@contextmanager
def password_capacity(directory=None):
    if getattr(_held, "active", False):
        yield
        return
    if not _process_slot.acquire(blocking=False):
        raise LoginBusy
    try:
        directory = directory or os.environ.get("GIFTLEDGER_LOGIN_LOCK_DIR")
        fd = None
        try:
            if directory:
                import fcntl

                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise LoginBusy from exc
            _held.active = True
            yield
        finally:
            _held.active = False
            if fd is not None:
                os.close(fd)
    finally:
        _process_slot.release()


uses_password_budget = password_capacity()


@dataclass(eq=False, slots=True)
class Attempt:
    time: float
    peer: str | None
    account: str


class FailureLimiter:
    def __init__(self, clock=time.monotonic, *, login_directory=None):
        self.clock = clock
        self.window = 600
        self.lock = Lock()
        self.login_directory = login_directory
        self.attempts = {"login": deque(), "invite": deque()}

    def password_check(self):
        return password_capacity(self.login_directory)

    @contextmanager
    def attempt(self, kind: str, peer: str | None, account: str, failure: type[Exception]):
        records = self.attempts[kind]
        with self.lock:
            now = self.clock()
            while records and records[0].time <= now - self.window:
                records.popleft()
            # The overall cap also bounds memory when clients rotate account names.
            groups = [(records, 240)]
            if peer is not None:
                groups.append(([item for item in records if item.peer == peer], 60))
            if kind == "login":
                groups.append(([item for item in records if item.account == account], 8))
            for entries, limit in groups:
                if len(entries) >= limit:
                    raise AttemptLimitReached(
                        max(1, math.ceil(entries[0].time + self.window - now))
                    )
            ticket = Attempt(now, peer, account)
            records.append(ticket)
        try:
            yield
        except failure:
            raise
        except BaseException:
            self.release(records, ticket)
            raise
        else:
            self.release(records, ticket)

    def release(self, records, ticket):
        with self.lock:
            if ticket in records:
                records.remove(ticket)
