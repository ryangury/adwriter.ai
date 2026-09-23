from __future__ import annotations

import os
import time
from pathlib import Path


class ScraperBusyError(Exception):
    """Raised when another process already holds the scraper lock."""


# Deliberately NOT os.kill(pid, 0) on Windows. There, signal 0 is
# signal.CTRL_C_EVENT, so os.kill(pid, 0) calls GenerateConsoleCtrlEvent: it
# fails with OSError when the target doesn't share our console (so a live lock
# holder looks dead and its lock gets deleted as "stale"), and when it does
# share our console it actually sends that process a Ctrl+C. Instead, open the
# process read-only and ask whether it has exited. POSIX keeps the usual
# signal-0 probe, which there really is a no-op existence check.
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def _pid_running(pid: int) -> bool:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Access denied means the process exists but belongs to someone else.
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # can't tell — err on the side of "held"
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

else:

    def _pid_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True  # exists, owned by another user
        except OSError:
            return False
        return True


def acquire_scraper_lock(lock_path: Path, *, wait_seconds: int = 0) -> None:
    """Raise ScraperBusyError if another live process holds the lock.
    A stale lock (PID no longer running) is cleaned up automatically.
    If wait_seconds > 0, poll and retry for up to that long before giving up.
    Reentrant per process: a lock file already holding this process's own PID
    is a no-op, so a nested acquire never waits on itself."""
    own_pid = os.getpid()
    deadline = time.monotonic() + wait_seconds
    while True:
        if lock_path.exists():
            try:
                pid = int(lock_path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pid = None
            if pid == own_pid:
                return  # already held by this process — reentrant no-op
            running = pid is not None and _pid_running(pid)
            if running:
                if time.monotonic() >= deadline:
                    raise ScraperBusyError(f"Scraper busy (held by PID {pid})")
                time.sleep(2)
                continue
            lock_path.unlink(missing_ok=True)
        lock_path.write_text(str(own_pid), encoding="utf-8")
        return


def release_scraper_lock(lock_path: Path) -> None:
    lock_path.unlink(missing_ok=True)


def release_lock_if_owned(lock_path: Path) -> bool:
    """Remove the lock file only if it still holds this process's PID, so a
    late or crashed run can never delete a lock another process now holds.
    Returns True if the file was ours and was removed."""
    try:
        pid = int(lock_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    if pid != os.getpid():
        return False
    lock_path.unlink(missing_ok=True)
    return True


SCRAPER_LOCK_PATH = Path("C:/adwriter/scraper.lock")
# Held by the orchestrator for its whole run (and by the standalone verifier
# for its run) — scraper.lock only covers individual browser sessions.
ORCHESTRATOR_LOCK_PATH = Path("C:/adwriter/orchestrator.lock")
