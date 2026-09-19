from __future__ import annotations

import os
import time
from pathlib import Path


class ScraperBusyError(Exception):
    """Raised when another process already holds the scraper lock."""


def acquire_scraper_lock(lock_path: Path, *, wait_seconds: int = 0) -> None:
    """Raise ScraperBusyError if another live process holds the lock.
    A stale lock (PID no longer running) is cleaned up automatically.
    If wait_seconds > 0, poll and retry for up to that long before giving up."""
    deadline = time.monotonic() + wait_seconds
    while True:
        if lock_path.exists():
            try:
                pid = int(lock_path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pid = None
            running = False
            if pid is not None:
                try:
                    os.kill(pid, 0)
                except OSError:
                    running = False
                else:
                    running = True
            if running:
                if time.monotonic() >= deadline:
                    raise ScraperBusyError(f"Scraper busy (held by PID {pid})")
                time.sleep(2)
                continue
            lock_path.unlink(missing_ok=True)
        lock_path.write_text(str(os.getpid()), encoding="utf-8")
        return


def release_scraper_lock(lock_path: Path) -> None:
    lock_path.unlink(missing_ok=True)


SCRAPER_LOCK_PATH = Path("C:/adwriter/scraper.lock")
