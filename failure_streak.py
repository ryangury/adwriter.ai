"""failure_streak.py — stop a per-vehicle loop once the same failure repeats.

When ACV Max itself is broken (signed out, page changed), every vehicle in a
loop fails with the same message and only the vehicle id differs. The 10/3/2026
run logged 83 identical "Merchandising iframe ... never attached" failures
before it finished. FailureStreak lets a loop stop after a few of those and
report one reason instead.
"""
from __future__ import annotations

import re

IDENTICAL_FAILURE_LIMIT = 5

# Any token containing a digit (vehicle ids, stock numbers, VINs, counts) is
# masked, so "... for 86606789 never attached" and "... for 86674475 never
# attached" are the same failure.
_ID_TOKEN_RE = re.compile(r"\S*\d\S*")


def failure_key(message: str) -> str:
    return _ID_TOKEN_RE.sub("#", (message or "").strip())


class FailureStreak:
    """Consecutive identical failures in one loop. Call ok() after any
    iteration that did not fail this way, fail(message) after one that did;
    fail() returns True once `limit` identical failures have run back to back."""

    def __init__(self, loop: str, limit: int = IDENTICAL_FAILURE_LIMIT) -> None:
        self.loop = loop
        self.limit = limit
        self._key: str | None = None
        self.count = 0
        self.last_message: str | None = None

    def ok(self) -> None:
        self._key = None
        self.count = 0

    def fail(self, message: str) -> bool:
        key = failure_key(message)
        self.count = self.count + 1 if key == self._key else 1
        self._key = key
        self.last_message = message
        return self.count >= self.limit

    @property
    def reason(self) -> str:
        """The repeated failure, for the alert subject."""
        return self.last_message or ""
