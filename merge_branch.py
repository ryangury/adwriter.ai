#!/usr/bin/env python3
"""merge_branch.py — merge branches into main in C:\\adwriter, run the gates, then restart the website.

    python merge_branch.py BRANCH [BRANCH ...]          # merge, gates, restart (never pushes)
    python merge_branch.py BRANCH --wait 60             # wait up to 60 minutes for a clear restart window
    python merge_branch.py BRANCH --no-restart          # stop after the gates (the banner then says restart needed)

Refused unless main is checked out with no modified tracked files, no orchestrator.lock / scraper.lock exists, no
verifier pass is due within 10 minutes and it is before 6:00 PM. Each branch is merged in order (a fast-forward when
possible, else a merge commit). Gates on the result: py_compile of every changed .py, python tests/run_tests.py (which
includes the stub full run, test_posting_order, also reported on its own), check_pages.py. A failed gate stops the
script before the restart and prints how to undo the merge. The last step is restart_site.restart(): its lock and
verifier-window checks, the restart, a signed-in render of /research and /inventory, and the website's start time
and commit. Pushing is a separate step: git push origin main.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import restart_site  # noqa: E402

CUTOFF_HOUR = 18


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=300)
    if check and r.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stdout.strip()} {r.stderr.strip()}")
    return r.stdout.strip()


def preconditions(now: datetime) -> list[str]:
    why = []
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        why.append("main is not checked out")
    if git("status", "--porcelain", "--untracked-files=no"):
        why.append("modified tracked files")
    if now.hour >= CUTOFF_HOUR:
        why.append("it is after 6:00 PM")
    nxt, last = restart_site.verifier_times()
    why += [w for w in restart_site.blockers(now, next_verifier=nxt, last_verifier=last) if "7:50" not in w]
    return why


def run(label: str, cmd: list[str], timeout: int = 1800) -> bool:
    print(f"[merge] {label}: {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    lines = (r.stdout + r.stderr).strip().splitlines()
    tail = [ln for ln in lines if ln.startswith(("FAIL", "ERROR", "checked ", "Ran ", "OK", "ALL PASSED"))
            or " passed, " in ln][-12:] or lines[-5:]
    for ln in tail:
        print(f"    {ln}")
    print(f"[merge] {label}: {'ok' if r.returncode == 0 else f'FAILED (exit {r.returncode})'}", flush=True)
    return r.returncode == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("branches", nargs="+")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--wait", type=int, default=0, metavar="MIN", help="wait up to MIN minutes for a restart window")
    args = ap.parse_args(argv)
    why = preconditions(datetime.now())
    if why:
        print("[merge] refused: " + "; ".join(why))
        return 1
    before = git("rev-parse", "HEAD")
    for b in args.branches:
        if subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", b], cwd=ROOT).returncode == 0:
            git("merge", "--ff-only", b)
            print(f"[merge] {b}: fast-forward to {git('rev-parse', '--short', 'HEAD')}")
        else:
            git("merge", "--no-edit", b)
            print(f"[merge] {b}: merge commit {git('rev-parse', '--short', 'HEAD')}")
    changed = [f for f in git("diff", "--name-only", before, "HEAD").splitlines() if f.endswith(".py")
               and (ROOT / f).exists()]
    ok = run("py_compile", [sys.executable, "-m", "py_compile", *changed]) if changed else True
    ok = run("tests", [sys.executable, "tests/run_tests.py"]) and ok
    ok = run("stub full run", [sys.executable, "tests/run_tests.py", "posting_order"]) and ok
    ok = run("check_pages", [sys.executable, "check_pages.py"]) and ok
    if not ok:
        print(f"[merge] a gate failed: NOT restarted, NOT pushed. Undo with: git reset --hard {before[:12]}")
        return 1
    print(f"[merge] main {before[:7]} -> {git('rev-parse', '--short', 'HEAD')}; gates passed")
    if args.no_restart:
        print("[merge] --no-restart: the website keeps its old code (the banner says restart needed)")
        return 0
    rc = restart_site.restart(wait_min=args.wait)
    print("[merge] not pushed; when approved: git push origin main")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
