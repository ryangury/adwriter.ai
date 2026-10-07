#!/usr/bin/env python3
"""restore_removed.py — put an archived ad_history entry back when its rebuild failed.

    python restore_removed.py --only P97084[,PM93165]   # restore these
    python restore_removed.py --all                    # every archived stock with no current entry
    ... --archive ad_history_removed_2026-10-07.json   # default: the newest ad_history_removed_*.json
    ... --dry-run                                      # show what would be restored, write nothing

An entry is restored only when ad_history.json has NO entry for that stock (a
rebuild that did run is never overwritten). Backs up ad_history.json first and
refuses while another process holds orchestrator.lock (a hand edit's own
holder can be named in ADWRITER_LOCK_HOLDER). The restored entry is the
archived one exactly as it was, so its old ad and dates come back; the stock is
not re-queued for a build while it has an entry.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import adwriter as A
from run_lock import ORCHESTRATOR_LOCK_PATH, lock_blocks_edit

HERE = Path(__file__).resolve().parent


def newest_archive() -> Path:
    files = sorted(HERE.glob("ad_history_removed_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        sys.exit("no ad_history_removed_*.json archive found")
    return files[-1]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--only", help="STOCK[,STOCK...]")
    sel.add_argument("--all", action="store_true")
    ap.add_argument("--archive", help="archive file (default: newest ad_history_removed_*.json)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    archive_path = Path(args.archive) if args.archive else newest_archive()
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    wanted = sorted(archive) if args.all else [s.strip().upper() for s in args.only.split(",") if s.strip()]
    history = A.load_ad_history()
    restore, skipped = [], []
    for s in wanted:
        if s not in archive:
            skipped.append((s, f"not in {archive_path.name}"))
        elif s in history:
            skipped.append((s, "has a current entry (rebuilt); left as is"))
        else:
            restore.append(s)
    print(f"archive: {archive_path.name}")
    for s in restore:
        print(f"  restore {s} (first ad {archive[s].get('first_ad_date')}, last ad {archive[s].get('last_ad_date')})")
    for s, why in skipped:
        print(f"  skip    {s}: {why}")
    if args.dry_run or not restore:
        print("dry run: nothing written" if args.dry_run else "nothing to restore")
        return 0
    holder = lock_blocks_edit(ORCHESTRATOR_LOCK_PATH)
    if holder is not None:
        sys.exit(f"orchestrator.lock is held by PID {holder}; not restoring.")
    backup = A.AD_HISTORY_PATH.with_name(f"ad_history.json.backup-{datetime.now():%Y-%m-%d-%H%M%S}-restore")
    shutil.copy2(A.AD_HISTORY_PATH, backup)
    print(f"backed up ad_history.json -> {backup.name}")
    history = A.load_ad_history()
    for s in restore:
        if s not in history:
            history[s] = archive[s]
    A.save_ad_history(history)
    print(f"restored {len(restore)} entr{'y' if len(restore) == 1 else 'ies'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
