"""log_stamp.py — prefix every line written to stdout / stderr with the local
time, so a log shows when each step started and where a run went quiet.

    import log_stamp; log_stamp.install()

Idempotent. Wraps the streams in place (print, tracebacks and library output
all go through them); a partial line gets its stamp when it starts, not when it
ends.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime


class _Stamped:
    def __init__(self, stream, clock=datetime.now):
        self._s = stream
        self._clock = clock
        self._at_line_start = True
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            out = []
            for piece in text.splitlines(keepends=True):
                if self._at_line_start:
                    out.append(self._clock().strftime("%H:%M:%S "))
                out.append(piece)
                self._at_line_start = piece.endswith("\n")
            self._s.write("".join(out))
        return len(text)

    def __getattr__(self, name):
        return getattr(self._s, name)


def install(clock=datetime.now) -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not None and not isinstance(stream, _Stamped):
            setattr(sys, name, _Stamped(stream, clock))
