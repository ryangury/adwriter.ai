#!/usr/bin/env python3
"""copy_dates.py — after a delete-and-rebuild, give the new entry its original first_ad_date.

    python copy_dates.py                     # dry run: what would change
    python copy_dates.py --apply             # write it
    ... --archive ad_history_removed_2026-10-07.json   (whose stocks to fix; default:
                                                        the newest ad_history_removed_*.json)
    ... --only STOCK[,STOCK...]

For every stock in that archive that now has a rebuilt entry, first_ad_date is set to
the earliest first_ad_date any archive holds for it (a car deleted twice is in
two archives; the older one has the original date), when that is earlier. Nothing else changes:
last_ad_date (always the rebuild's own, later date) is never touched, nor is the
ad text, so no repost is triggered. Backs up ad_history.json before --apply and
refuses while another process holds orchestrator.lock (a hand edit's own holder
can be named in ADWRITER_LOCK_HOLDER).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import adwriter as A
from list_not_rebuilt import HERE, earliest_first_ad_date
from run_lock import ORCHESTRATOR_LOCK_PATH, lock_blocks_edit


def plan(history: dict, stocks: set[str], archives: list[Path], only: set[str] | None = None) -> list[tuple[str, str, str]]:
    """[(stock, current first_ad_date, earliest first_ad_date in any archive)] to change."""
    out = []
    for s in sorted(stocks):
        if only and s not in only:
            continue
        cur = history.get(s)
        was = earliest_first_ad_date(s, archives)
        if not cur or not was:
            continue
        now = cur.get("first_ad_date")
        if not now or was < now:
            out.append((s, now, was))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--archive")
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    archives = sorted(HERE.glob("ad_history_removed_*.json"))
    if not archives:
        sys.exit("no ad_history_removed_*.json archive found")
    target = Path(args.archive) if args.archive else max(archives, key=lambda p: p.stat().st_mtime)
    stocks = set(json.loads(target.read_text(encoding="utf-8")))
    only = {s.strip().upper() for s in args.only.split(",") if s.strip()} or None
    history = A.load_ad_history()
    changes = plan(history, stocks, archives, only)
    not_rebuilt = sorted(s for s in stocks if s not in history and (not only or s in only))
    print(f"stocks from {target.name}; dates from {len(archives)} archive(s)")
    for s, now, was in changes:
        print(f"  {s}: first_ad_date {now} -> {was} (last_ad_date stays {history[s].get('last_ad_date')})")
    if not_rebuilt:
        print(f"  not rebuilt yet (no entry): {', '.join(not_rebuilt)}")
    if not args.apply:
        print(f"dry run: {len(changes)} to change, nothing written")
        return 0
    if not changes:
        print("nothing to change")
        return 0
    holder = lock_blocks_edit(ORCHESTRATOR_LOCK_PATH)
    if holder is not None:
        sys.exit(f"orchestrator.lock is held by PID {holder}; not applying.")
    backup = A.AD_HISTORY_PATH.with_name(f"ad_history.json.backup-{datetime.now():%Y-%m-%d-%H%M%S}-copy-dates")
    shutil.copy2(A.AD_HISTORY_PATH, backup)
    print(f"backed up ad_history.json -> {backup.name}")
    history = A.load_ad_history()
    for s, _now, was in plan(history, stocks, archives, only):
        history[s]["first_ad_date"] = was
    A.save_ad_history(history)
    print(f"applied to {len(changes)} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
