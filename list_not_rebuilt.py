#!/usr/bin/env python3
"""list_not_rebuilt.py — archived (deleted-for-rebuild) stocks that still have no ad.

    python list_not_rebuilt.py                 # newest orchestrator log, archives from the last 7 days
    python list_not_rebuilt.py --log orchestrator_logs/orchestrator_20261008_050002.log
    python list_not_rebuilt.py --archive ad_history_removed_2026-10-07.json

A stock is listed when an ad_history_removed_*.json archive holds it, it is in
this morning's inventory snapshot, and ad_history.json has no entry for it.
The reason comes from that run's log lines for the stock: status 1, recon open,
pricing gate, no sticker, error, absent, or "not reached" when the log never
mentions it. The orchestrator prints this right after its build step (before
CTR) and lists the stocks in the Action Required email. restore_removed.py puts
an archived entry back.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "orchestrator_logs"
ARCHIVE_MAX_AGE_DAYS = 7

_DECIDING_TAG_RE = re.compile(r"\[(?:gate|recon-gate|build|pre-recon|orchestrator|aggregator|adwriter|cache|scraper)\]")
# (reason, pattern on the stock's log lines), checked in order.
_REASONS = [
    ("status 1 (needs certification)", re.compile(r"status 1\b")),
    ("status not buildable", re.compile(r"status \d+ not in")),
    ("absent from inventory", re.compile(r"absent since|absent from inventory")),
    ("pricing gate (ACV Max price not ready)", re.compile(r"pricing not ready|price is 0")),
    ("no sticker", re.compile(r"sticker|AutoiPacket|autoipacket|window_sticker|msrp", re.I)),
    ("recon open / not checked", re.compile(r"-> waiting|recon flipped incomplete|recon check failed|ReconVision (?:timeout|unreachable)")),
    ("error", re.compile(r"failed|error|failure|Traceback", re.I)),
]


def default_archives(max_age_days: int = ARCHIVE_MAX_AGE_DAYS) -> list[Path]:
    cutoff = time.time() - max_age_days * 86400
    return sorted(p for p in HERE.glob("ad_history_removed_*.json") if p.stat().st_mtime >= cutoff)


def earliest_first_ad_date(stock: str, archives: list[Path]) -> str | None:
    """The earliest first_ad_date any archive holds for `stock` (a car deleted
    and rebuilt twice is in two archives; the older one has the original date)."""
    dates = []
    for path in archives:
        try:
            d = (json.loads(path.read_text(encoding="utf-8")).get(stock) or {}).get("first_ad_date")
        except (OSError, ValueError):
            continue
        if d:
            dates.append(d)
    return min(dates) if dates else None


def default_log() -> Path | None:
    env = os.environ.get("LOGFILE")  # set by run_orchestrator.bat for this run
    if env and Path(env).exists():
        return Path(env)
    logs = sorted(LOG_DIR.glob("orchestrator_*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def reason_for(stock: str, log_text: str) -> tuple[str, str]:
    """(reason, the deciding log line)."""
    # Only the gate / build lines decide (CTR, benchmark, verify and email lines don't).
    lines = [ln for ln in log_text.splitlines()
             if re.search(rf"\b{re.escape(stock)}\b", ln) and _DECIDING_TAG_RE.search(ln)]
    if not lines:
        return "not reached (the run never got to it)", ""
    for reason, rx in _REASONS:
        for ln in lines:
            if rx.search(ln):
                return reason, ln.strip()
    return "no ad recorded (see log)", lines[-1].strip()


def not_rebuilt(
    history: dict[str, Any], archives: list[Path], inventory: set[str] | None, log_text: str
) -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for path in sorted(archives, key=lambda p: p.stat().st_mtime, reverse=True):  # newest archive names the row
        try:
            archived = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for stock, old in archived.items():
            if stock in history or stock in seen:
                continue
            if inventory is not None and stock.upper() not in inventory:
                continue
            reason, line = reason_for(stock, log_text)
            seen[stock] = {"stock": stock, "archive": path.name, "first_ad_date": earliest_first_ad_date(stock, archives) or "",
                           "reason": reason, "log_line": line}
    return [seen[s] for s in sorted(seen)]


def _inventory() -> set[str] | None:
    try:
        snap = json.loads((HERE / "last_inventory_snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    vehicles = snap.get("vehicles") if isinstance(snap, dict) else snap
    return {str(v.get("stock_number")).strip().upper() for v in vehicles or [] if v.get("stock_number")} or None


def report(history: dict[str, Any], log_path: Path | None = None, archives: list[Path] | None = None) -> list[dict[str, str]]:
    log_path = log_path or default_log()
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path and log_path.exists() else ""
    return not_rebuilt(history, archives if archives is not None else default_archives(), _inventory(), log_text)


def print_report(rows: list[dict[str, str]]) -> None:
    print(f"[not-rebuilt] deleted for rebuild, no new ad: {len(rows)}")
    for r in rows:
        print(f"[not-rebuilt]   {r['stock']}: {r['reason']} (archived in {r['archive']}, first ad {r['first_ad_date']})"
              + (f" | {r['log_line'][:160]}" if r["log_line"] else ""))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log")
    ap.add_argument("--archive", action="append")
    args = ap.parse_args(argv)
    from adwriter import load_ad_history

    rows = report(load_ad_history(), Path(args.log) if args.log else None,
                  [Path(a) for a in args.archive] if args.archive else None)
    print_report(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
