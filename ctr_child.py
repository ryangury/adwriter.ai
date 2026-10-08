#!/usr/bin/env python3
"""ctr_child.py — one CTR step in its own process, for the orchestrator's watchdog.

    python ctr_child.py durham    --input in.json --result out.json --progress p.json [--skip S1,S2]
    python ctr_child.py benchmark --store "Hendrick Motors of Charlotte" --result out.json --progress p.json
    python ctr_child.py restore-durham --result out.json

durham: in.json holds {"retail": [...], "aggregated_ctr": {...}} from the run.
benchmark: one store per child, so a hang at one store never costs the other.
restore-durham: after a benchmark child was killed mid-store, sign in and
switch the ACV Max account back to Mercedes-Benz of Durham.

Every vehicle is recorded to ctr_history.db as soon as it is read and the
progress file (step_watchdog.ProgressWriter) is updated, so the parent can kill
this process at any moment and lose only the vehicle in flight. The result
file holds the counts and errors: {"counts", "errors", "aborted"}.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import log_stamp


def _write(path: str | None, data: dict[str, Any]) -> None:
    if path:
        Path(path).write_text(json.dumps(data, default=str), encoding="utf-8")


def main(argv: list[str]) -> int:
    log_stamp.install()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step", choices=("durham", "benchmark", "restore-durham"))
    ap.add_argument("--store")
    ap.add_argument("--input")
    ap.add_argument("--result")
    ap.add_argument("--progress")
    ap.add_argument("--skip", default="")
    ap.add_argument("--done", default="", help="stocks an earlier attempt already finished (not read again)")
    ap.add_argument("--trace-after", type=float, default=0,
                    help="dump every thread's stack to stderr after this many seconds (and every N s after): "
                         "shows where a hang sits")
    args = ap.parse_args(argv)
    if args.trace_after > 0:
        import faulthandler

        faulthandler.dump_traceback_later(args.trace_after, repeat=True, file=sys.stderr)

    from ctr_warmup import capture_benchmark_ctr, capture_durham_ctr
    from failure_streak import FailureStreak
    from scraper import ACVMAX_DEALERSHIP, ACVMaxScraper, AcvMaxRunAbort
    from step_watchdog import ProgressWriter

    progress = ProgressWriter(args.progress)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    already = {s.strip() for s in args.done.split(",") if s.strip()}
    errors: list[dict[str, Any]] = []
    result: dict[str, Any] = {"counts": None, "errors": errors, "aborted": None}
    try:
        with ACVMaxScraper(headless=True) as ax:
            progress.beat("login")
            ax.login()
            progress.beat("logged in")
            if args.step == "restore-durham":
                if ax.current_dealership() != ACVMAX_DEALERSHIP:
                    ax.switch_dealership(ACVMAX_DEALERSHIP)
                result["counts"] = {"dealership": ax.require_durham("restore after a killed benchmark")}
            elif args.step == "durham":
                data = json.loads(Path(args.input).read_text(encoding="utf-8"))
                from adwriter import load_ad_history

                result["counts"] = capture_durham_ctr(
                    ax, data.get("retail") or [], ad_history=load_ad_history(),
                    aggregated_ctr=data.get("aggregated_ctr") or {}, errors=errors,
                    streak=FailureStreak("ctr"), skip=skip, already=already, progress=progress,
                )
                if result["counts"].get("aborted"):
                    result["aborted"] = result["counts"]["aborted"]
            else:
                result["counts"] = capture_benchmark_ctr(ax, errors=errors, stores=[args.store], skip=skip,
                                                         already=already, progress=progress)
                # Never leave the account on another store.
                ax.require_durham(f"after the {args.store} benchmark")
    except AcvMaxRunAbort as exc:
        result["aborted"] = str(exc)
        result["abort_type"] = type(exc).__name__
        print(f"[ctr-child] {args.step}: ACV Max stopped — {exc}", file=sys.stderr)
        _write(args.result, result)
        return 3
    except Exception as exc:  # noqa: BLE001 - reported to the parent through the result file
        errors.append({"stock": "-", "phase": f"{args.step}_child", "error": f"{type(exc).__name__}: {exc}"})
        print(f"[ctr-child] {args.step}: failed — {exc}", file=sys.stderr)
        _write(args.result, result)
        return 1
    _write(args.result, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
