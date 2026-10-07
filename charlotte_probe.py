#!/usr/bin/env python3
"""charlotte_probe.py — run the Charlotte benchmark alone, to see whether the
2026-10-07 hang reproduces and where.

    python charlotte_probe.py [--budget 600] [--trace-after 300] [--wait 1800]

Waits (up to --wait seconds) until no live process holds orchestrator.lock or
scraper.lock (the verifier and the sticker warmup hold them while they use the
ACV Max login), then takes orchestrator.lock so neither starts meanwhile, and
runs `ctr_child.py benchmark --store "Hendrick Motors of Charlotte"` under the
step watchdog: a --budget limit (10 minutes), a vehicle over 90 s is skipped,
and faulthandler dumps every thread's stack after --trace-after seconds (and
again every --trace-after seconds), so a hang shows the exact call it sits in.
Afterwards the account is switched back to Durham if the child was killed.
Output: orchestrator_logs/charlotte_probe_<stamp>.log. Records CTR rows for the
vehicles it reads, like a normal run.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE = "Hendrick Motors of Charlotte"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--budget", type=float, default=600)
    ap.add_argument("--trace-after", type=float, default=300)
    ap.add_argument("--wait", type=float, default=1800)
    args = ap.parse_args(argv)

    log_path = HERE / "orchestrator_logs" / f"charlotte_probe_{datetime.now():%Y%m%d_%H%M%S}.log"
    log_path.parent.mkdir(exist_ok=True)
    log = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    import log_stamp

    log_stamp.install()
    from run_lock import ORCHESTRATOR_LOCK_PATH, SCRAPER_LOCK_PATH, acquire_scraper_lock, lock_blocks_edit, \
        release_lock_if_owned
    from step_watchdog import python_argv, run_watched

    deadline = time.monotonic() + args.wait
    while True:
        busy = {p.name: lock_blocks_edit(p) for p in (ORCHESTRATOR_LOCK_PATH, SCRAPER_LOCK_PATH)}
        busy = {k: v for k, v in busy.items() if v is not None}
        if not busy:
            break
        if time.monotonic() > deadline:
            print(f"[probe] still busy after {args.wait:.0f}s ({busy}) - not running")
            return 2
        print(f"[probe] waiting: {busy}")
        time.sleep(30)
    acquire_scraper_lock(ORCHESTRATOR_LOCK_PATH, wait_seconds=0)
    print(f"[probe] holding orchestrator.lock; Charlotte benchmark alone, budget {args.budget:.0f}s, "
          f"stack dumps every {args.trace_after:.0f}s")
    work = HERE / "orchestrator_logs"
    try:
        def child(step, *extra):
            return run_watched(
                lambda skipped: python_argv(HERE / "ctr_child.py", step, *extra,
                                            "--result", str(work / "charlotte_probe_result.json"),
                                            "--progress", str(work / "charlotte_probe_progress.json"),
                                            *(["--skip", ",".join(skipped)] if skipped else [])),
                step=f"probe {step}", progress_path=work / "charlotte_probe_progress.json", budget_s=args.budget,
            )

        t0 = time.monotonic()
        res = child("benchmark", "--store", STORE, "--trace-after", str(args.trace_after))
        print(f"[probe] result: {res} after {time.monotonic() - t0:.0f}s")
        if res["status"] != "ok":
            fix = child("restore-durham")
            print(f"[probe] restore Durham: {fix['status']}")
        return 0 if res["status"] == "ok" else 1
    finally:
        release_lock_if_owned(ORCHESTRATOR_LOCK_PATH)
        print("[probe] released orchestrator.lock")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
