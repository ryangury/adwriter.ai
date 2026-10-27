#!/usr/bin/env python3
"""site_version.py — what code the running website is, and whether it is stale.

app.py imports this at startup, so START_TIME and RUNNING_COMMIT are the process's own. A process that keeps running
after a merge mixes code: modules it already imported stay old while a module first imported later comes from the new
files (that is how /research failed from 10/26 with "towing_for() got an unexpected keyword argument 'vin_decode'":
the new tow_refresh met the old towing). app.py therefore imports every lazily loaded module at startup
(EAGER_MODULES), and every signed-in page shows a "restart needed" banner when staleness() finds that
    - HEAD is no longer the commit the process started on, or
    - a tracked code file (top-level *.py, templates/, static/) changed after the process started.
HEAD is read from the .git files themselves (no git command: the website runs as LocalSystem, which git may refuse
as a "dubious ownership" repository). staleness() is cached for CHECK_TTL_S.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CHECK_TTL_S = 30.0

# Every project module some code path imports inside a function (found by tests/test_site_version.py's scan): a
# module imported first after a merge would be the new code next to old ones. app.py imports them all at startup.
EAGER_MODULES = (
    "ad_timeline", "adwriter", "aggregator", "api_cost", "bounded_search", "carfax_refresh", "cost_summary",
    "ctr_database", "ctr_report", "ctr_warmup", "email_config", "failure_streak", "feature_cache",
    "inventory_crawler", "log_stamp", "model_config", "powertrain", "research", "run_lock", "scraper",
    "site_version", "step_watchdog", "tow_refresh", "towing", "vehicle_cache", "vin_decode", "vin_importer",
    "vision_parser",
)


def _git_dir(root: Path) -> Path | None:
    g = root / ".git"
    if g.is_dir():
        return g
    if g.is_file():                                   # a worktree: "gitdir: <path>"
        text = g.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            return Path(text.split(":", 1)[1].strip())
    return None


def head_commit(root: Path | None = None) -> str | None:
    """The full commit HEAD points to, read from the .git files (None when unreadable)."""
    try:
        g = _git_dir(root or ROOT)
        if not g:
            return None
        head = (g / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head.split(":", 1)[1].strip()
        common = g
        if (g / "commondir").exists():
            common = (g / (g / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for base in (g, common):
            p = base / ref
            if p.exists():
                return p.read_text(encoding="utf-8").strip() or None
        packed = common / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except OSError:
        return None
    return None


def code_files(root: Path | None = None) -> list[Path]:
    """The files the website runs: top-level *.py, templates/ and static/."""
    root = root or ROOT
    out = sorted(root.glob("*.py"))
    for sub in ("templates", "static"):
        out += sorted(p for p in (root / sub).rglob("*") if p.is_file())
    return out


START_TIME = time.time()
RUNNING_COMMIT = head_commit()
_cache: dict[str, Any] = {"at": 0.0, "value": None}


def staleness(*, root: Path | None = None, started: float | None = None, running: str | None = None,
              now: float | None = None, use_cache: bool = True) -> dict[str, Any]:
    """{"stale", "reasons", "running_commit", "head_commit", "started", "newer_files"}."""
    now = now or time.time()
    if use_cache and root is None and _cache["value"] is not None and now - _cache["at"] < CHECK_TTL_S:
        return _cache["value"]
    root = root or ROOT
    started = START_TIME if started is None else started
    running = RUNNING_COMMIT if running is None else running
    head = head_commit(root)
    reasons = []
    if head and running and head != running:
        reasons.append(f"main is now {head[:7]}; the website is running {running[:7]}")
    newer = []
    for p in code_files(root):
        try:
            if p.stat().st_mtime > started + 1:
                newer.append(str(p.relative_to(root)).replace("\\", "/"))
        except OSError:
            continue
    if newer:
        reasons.append(f"{len(newer)} code file(s) changed after the website started ({', '.join(newer[:3])}"
                       f"{', ...' if len(newer) > 3 else ''})")
    value = {"stale": bool(reasons), "reasons": reasons, "running_commit": running, "head_commit": head,
             "started": datetime.fromtimestamp(started).isoformat(timespec="seconds"), "newer_files": newer}
    if root == ROOT:
        _cache.update(at=now, value=value)
    return value
