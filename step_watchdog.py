"""step_watchdog.py — run a browser step in a child process the orchestrator can kill.

A Playwright call that never returns (the 2026-10-07 Charlotte benchmark sat
40 minutes on one call) cannot be interrupted from inside the process, so CTR
capture and the benchmark run as children (ctr_child.py) and this parent:

  * reads the child's progress file (ProgressWriter): which vehicle it is on
    and when it started;
  * kills the child's whole process tree (browser included) when
      - one vehicle runs longer than item_s (90 s): the child is restarted
        with that vehicle skipped, at most max_restarts times;
      - nothing progresses for idle_s (10 min): "hung";
      - the step runs longer than budget_s (45 min): "budget";
  * returns what happened; the caller logs it, sends one alert, and goes on
    to verification and the final emails.

Each vehicle is recorded by the child as soon as it is read, so a kill loses
only the vehicle in flight.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ITEM_LIMIT_S = 90
IDLE_LIMIT_S = 10 * 60
STEP_BUDGET_S = 45 * 60            # Durham CTR (76 vehicles took 30 minutes on 10/8)
BENCHMARK_BUDGET_S = 65 * 60       # per benchmark store (Northlake 44 min, Charlotte about 50 on 10/8)
MAX_RESTARTS = 5


# --- child side ------------------------------------------------------------------- #

class ProgressWriter:
    """The child's heartbeat: one small JSON file rewritten on every event."""

    def __init__(self, path: str | Path | None, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self.state: dict[str, Any] = {"pid": os.getpid(), "current": None, "current_started": None,
                                      "done": 0, "total": None, "done_labels": [], "updated_at": clock()}
        self._write()

    def _write(self) -> None:
        if not self.path:
            return
        self.state["updated_at"] = self.clock()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        os.replace(tmp, self.path)

    def start(self, label: str) -> None:
        self.state.update(current=label, current_started=self.clock())
        self._write()

    def done(self, label: str | None = None) -> None:
        """A vehicle finished (read or failed). Its label goes in done_labels so a
        restarted child can skip it instead of reading it again."""
        labels = self.state["done_labels"]
        if label is None:
            label = self.state.get("current")
        if label and label not in labels:
            labels.append(label)
        self.state.update(current=None, current_started=None, done=self.state["done"] + 1)
        self._write()

    def set_total(self, n: int) -> None:
        """How many vehicles this child will go through (known once the crawl is done)."""
        self.state["total"] = n
        self._write()

    def beat(self, note: str | None = None) -> None:
        """Progress that isn't a vehicle (login, store switch, crawl pages)."""
        if note:
            self.state["note"] = note
        self._write()


def read_progress(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- parent side ------------------------------------------------------------------ #

def kill_tree(pid: int) -> None:
    """Kill a process and everything under it (the Playwright driver and the
    browser are the child's descendants)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:  # pragma: no cover - the production host is Windows
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_watched(
    make_argv: Callable[[list[str]], list[str]],
    *,
    step: str,
    progress_path: Path,
    budget_s: float = STEP_BUDGET_S,
    idle_s: float = IDLE_LIMIT_S,
    item_s: float = ITEM_LIMIT_S,
    max_restarts: int = MAX_RESTARTS,
    poll_s: float = 2.0,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda m: print(m, flush=True),
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run make_argv(skipped, done) as a child until it exits, restarting it
    past a vehicle that overruns item_s. `skipped`: vehicles that overran;
    `done`: vehicles an earlier attempt already finished, so a restart never
    reads (or records) them twice. Returns {"status": "ok" | "failed" | "hung" |
    "budget", "returncode", "restarts", "skipped": [labels], "reason",
    "progress": {"done", "total", "current"}} - progress is what the child had
    finished when it ended or was killed, across restarts.

    "hung": no progress for idle_s (the child stopped). "budget": over budget_s
    while still making progress (it simply ran out of time)."""
    started = clock()
    skipped: list[str] = []
    done_labels: list[str] = []
    restarts = 0
    while True:
        Path(progress_path).unlink(missing_ok=True)
        argv = make_argv(skipped, list(done_labels))
        kw: dict[str, Any] = {"env": env}
        if os.name != "nt":  # pragma: no cover
            kw["start_new_session"] = True
        proc = subprocess.Popen(argv, **kw)
        last_beat = clock()
        last_seen: tuple | None = None
        log(f"[watchdog] {step}: child PID {proc.pid} started" + (f" (skipping {', '.join(skipped)})" if skipped else ""))
        while True:
            rc = proc.poll()
            prog = read_progress(progress_path) or {}
            summary = _progress_summary(prog, done_labels)
            if rc is not None:
                status = "ok" if rc == 0 else "failed"
                log(f"[watchdog] {step}: child exited with code {rc}")
                return {"status": status, "returncode": rc, "restarts": restarts, "skipped": skipped,
                        "reason": None if rc == 0 else f"{step} child exited with code {rc}", "progress": summary}
            now = clock()
            seen = (prog.get("updated_at"), prog.get("current"), prog.get("done"))
            if seen != last_seen:
                last_seen, last_beat = seen, now
            cur, cur_t = prog.get("current"), prog.get("current_started")
            if now - last_beat > idle_s:
                reason = f"{step} hung: no progress for {int(idle_s // 60)} minutes" + (f" (on {cur})" if cur else "")
                return _killed(proc, "hung", reason, restarts, skipped, log, summary)
            if now - started > budget_s:
                reason = (f"{step} ran out of time after {summary['done']}"
                          + (f" of {summary['total']}" if summary["total"] else "")
                          + f" vehicles ({int(budget_s // 60)}-minute budget; it was still making progress)")
                return _killed(proc, "budget", reason, restarts, skipped, log, summary)
            if cur and cur_t and now - cur_t > item_s:
                log(f"[watchdog] {step}: {cur} over the {int(item_s)}-second per-vehicle limit — killing the child, "
                    f"skipping it and restarting")
                kill_tree(proc.pid)
                proc.wait(timeout=30)
                skipped.append(cur)
                for lab in prog.get("done_labels") or []:
                    if lab not in done_labels:
                        done_labels.append(lab)
                restarts += 1
                if restarts > max_restarts:
                    return {"status": "hung", "returncode": None, "restarts": restarts, "skipped": skipped,
                            "reason": f"{step}: more than {max_restarts} vehicles over the per-vehicle limit",
                            "progress": _progress_summary({}, done_labels, prog.get("total"))}
                break
            sleep(poll_s)


def _progress_summary(prog: dict[str, Any], carried: list[str], total: int | None = None) -> dict[str, Any]:
    """{"done", "total", "current"}: vehicles finished across every attempt
    (earlier attempts' done_labels plus this child's own)."""
    labels = list(carried)
    for lab in prog.get("done_labels") or []:
        if lab not in labels:
            labels.append(lab)
    return {"done": len(labels), "total": prog.get("total") or total, "current": prog.get("current")}


def _killed(proc, status, reason, restarts, skipped, log, progress=None) -> dict[str, Any]:
    log(f"[watchdog] {reason} — killing child PID {proc.pid} and its browser")
    kill_tree(proc.pid)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    return {"status": status, "returncode": None, "restarts": restarts, "skipped": skipped, "reason": reason,
            "progress": progress or {"done": 0, "total": None, "current": None}}


def python_argv(script: str | Path, *args: str) -> list[str]:
    """argv for a child Python script, unbuffered so its output lands in the log."""
    return [sys.executable, "-u", str(script), *args]
