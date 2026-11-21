"""Bound failed or concurrent credential checks for the single-worker site API."""

import math
import os
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock


class AttemptLimitReached(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


class LoginBusy(Exception):
    pass


@dataclass(eq=False, slots=True)
class Attempt:
    time: float
    peer: str
    account: str


class FailureLimiter:
    def __init__(self, clock=time.monotonic, *, login_directory=None):
        self.clock = clock
        self.window = 600
        self.lock = Lock()
        self.login_directory = login_directory
        self.login_lock = Lock()
        self.attempts = {"login": deque(), "invite": deque()}

    @contextmanager
    def password_check(self):
        if not self.login_lock.acquire(blocking=False):
            raise LoginBusy
        try:
            if self.login_directory is None:
                # Standalone single-worker API, including earlier public YAML.
                yield
                return
            # Every API process of this Linux site sees the same app-data directory.
            # Its inode is the lock; no file is added to the ledger or its backups.
            import fcntl

            fd = os.open(self.login_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise LoginBusy from exc
                yield
            finally:
                os.close(fd)
        finally:
            self.login_lock.release()

    @contextmanager
    def attempt(self, kind: str, peer: str, account: str, failure: type[Exception]):
        records = self.attempts[kind]
        with self.lock:
            now = self.clock()
            while records and records[0].time <= now - self.window:
                records.popleft()
            # The overall cap also bounds memory when clients rotate account names.
            groups = [(records, 240), ([item for item in records if item.peer == peer], 60)]
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
