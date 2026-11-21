"""Bound failed or concurrent credential checks for the single-worker site API."""

import math
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock


class AttemptLimitReached(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


@dataclass(eq=False, slots=True)
class Attempt:
    time: float
    peer: str
    account: str


class FailureLimiter:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.window = 600
        self.lock = Lock()
        self.attempts = {"login": deque(), "invite": deque()}

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
