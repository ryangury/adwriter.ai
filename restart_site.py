#!/usr/bin/env python3
"""restart_site.py — restart the website safely, then prove it serves the new code.

    python restart_site.py                 # check, restart, verify (exit 1 on any failure)
    python restart_site.py --check-only    # print what would block a restart now
    python restart_site.py --wait 60       # wait up to 60 minutes for a clear window

A restart is refused while orchestrator.lock or scraper.lock exists, between 7:50 and 8:10, or within
VERIFIER_MARGIN_MIN minutes of an AdWriter-Verifier-Hourly pass (its next or last run time). Then
`schtasks /run /tn AdWriter-Restart`, a wait for a new website process, a signed-in render of /research and
/inventory (the shared password from credentials.py, never printed), and /version: the running commit must be HEAD
and the page not stale. The last step of merge_branch.py.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = "http://localhost:5000"
LOCKS = (ROOT / "orchestrator.lock", ROOT / "scraper.lock")
VERIFIER_TASK = "AdWriter-Verifier-Hourly"
VERIFIER_MARGIN_MIN = 10
QUIET = ((7, 50), (8, 10))            # the morning run's end and the 8:00 verifier
PAGES = ("/research", "/inventory")
START_WAIT_S = 180


def _ps(cmd: str) -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                          timeout=60).stdout.strip()


def verifier_times() -> tuple[datetime | None, datetime | None]:
    """(next run, last run) of the verifier task, or None where unknown."""
    out = _ps(f"$i = Get-ScheduledTaskInfo -TaskName '{VERIFIER_TASK}'; "
              "'{0}|{1}' -f $i.NextRunTime.ToString('s'), $i.LastRunTime.ToString('s')")
    vals = []
    for part in (out.split("|") + ["", ""])[:2]:
        try:
            vals.append(datetime.fromisoformat(part))
        except ValueError:
            vals.append(None)
    return vals[0], vals[1]


def blockers(now: datetime, *, locks: tuple[Path, ...] = LOCKS, next_verifier: datetime | None = None,
             last_verifier: datetime | None = None) -> list[str]:
    """Why a restart must wait now ([] = clear)."""
    out = [f"{p.name} exists (PID {p.read_text(encoding='utf-8', errors='replace').strip() or '?'})"
           for p in locks if p.exists()]
    (h1, m1), (h2, m2) = QUIET
    if now.replace(hour=h1, minute=m1, second=0, microsecond=0) <= now < now.replace(hour=h2, minute=m2, second=0,
                                                                                         microsecond=0):
        out.append(f"{h1}:{m1:02d}-{h2}:{m2:02d} (the morning run's end and the 8:00 verifier)")
    margin = timedelta(minutes=VERIFIER_MARGIN_MIN)
    if next_verifier and timedelta(0) <= next_verifier - now < margin:
        out.append(f"a verifier pass is due at {next_verifier:%H:%M}")
    if last_verifier and timedelta(0) <= now - last_verifier < margin:
        out.append(f"a verifier pass started at {last_verifier:%H:%M}")
    return out


def site_process() -> dict[str, str] | None:
    """The website's python process: {"pid", "started"} (the newest one running app.py), or None."""
    out = _ps("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
              "Where-Object { $_.CommandLine -match 'app\\.py' } | Sort-Object CreationDate -Descending | "
              "Select-Object -First 1 | ForEach-Object { '{0}|{1}' -f $_.ProcessId, $_.CreationDate.ToString('s') }")
    if "|" not in out:
        return None
    pid, started = out.split("|", 1)
    return {"pid": pid, "started": started}


def signed_in_check(pages: tuple[str, ...] = PAGES) -> dict[str, object]:
    """Sign in with the shared password (never printed), GET each page and /version."""
    sys.path.insert(0, str(ROOT))
    from credentials import DEMO_PASSWORD

    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open(SITE + "/auth", data=urllib.parse.urlencode({"password": DEMO_PASSWORD}).encode(), timeout=30).read()
    out: dict[str, object] = {}
    for page in pages:
        t0 = time.monotonic()
        try:
            r = opener.open(SITE + page, timeout=120)
            body = r.read().decode("utf-8", "replace")
            out[page] = {"status": r.status, "seconds": round(time.monotonic() - t0, 2),
                         "signed_in": 'name="password"' not in body, "banner": "restart-banner" in body}
        except urllib.error.HTTPError as exc:
            out[page] = {"status": exc.code, "seconds": round(time.monotonic() - t0, 2)}
    try:
        out["/version"] = json.loads(opener.open(SITE + "/version", timeout=30).read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        out["/version"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def head() -> str | None:
    sys.path.insert(0, str(ROOT))
    import site_version

    return site_version.head_commit(ROOT)


def restart(*, wait_min: int = 0) -> int:
    deadline = datetime.now() + timedelta(minutes=wait_min)
    while True:
        now = datetime.now()
        why = blockers(now, next_verifier=(v := verifier_times())[0], last_verifier=v[1])
        if not why:
            break
        if now >= deadline:
            print("[restart] NOT restarted: " + "; ".join(why))
            return 1
        print(f"[restart] waiting: {'; '.join(why)}", flush=True)
        time.sleep(60)
    before = site_process()
    issued = datetime.now()
    print(f"[restart] {issued:%H:%M:%S} schtasks /run /tn AdWriter-Restart (website was PID "
          f"{(before or {}).get('pid', '?')}, started {(before or {}).get('started', '?')})", flush=True)
    r = subprocess.run(["schtasks", "/run", "/tn", "AdWriter-Restart"], capture_output=True, text=True, timeout=60)
    if r.returncode:
        print(f"[restart] schtasks failed: {r.stdout.strip()} {r.stderr.strip()}")
        return 1
    proc = None
    for _ in range(START_WAIT_S // 3):
        time.sleep(3)
        proc = site_process()
        if proc and proc["started"] >= issued.strftime("%Y-%m-%dT%H:%M:%S")[:19] and (not before or proc["pid"] != before["pid"]):
            try:
                urllib.request.urlopen(SITE + "/", timeout=10).read()
                break
            except Exception:  # noqa: BLE001 - not answering yet
                continue
    else:
        print(f"[restart] FAILED: no new website process answering within {START_WAIT_S} s (now {proc})")
        return 1
    res = signed_in_check()
    ver = res.get("/version") or {}
    want = head()
    ok = all(isinstance(res.get(p), dict) and res[p].get("status") == 200 and res[p].get("signed_in") for p in PAGES) \
        and ver.get("running_commit") == want and not ver.get("stale")
    print(f"[restart] website PID {proc['pid']} started {proc['started']}, running commit "
          f"{(ver.get('running_commit') or '?')[:12]} (HEAD {(want or '?')[:12]})")
    for p in PAGES:
        print(f"[restart] signed-in {p}: {res.get(p)}")
    print(f"[restart] /version: stale={ver.get('stale')} {ver.get('reasons') or ''}")
    print("[restart] OK" if ok else "[restart] FAILED (see above)")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--wait", type=int, default=0, metavar="MIN", help="wait up to MIN minutes for a clear window")
    args = ap.parse_args(argv)
    if args.check_only:
        v = verifier_times()
        why = blockers(datetime.now(), next_verifier=v[0], last_verifier=v[1])
        print("clear to restart" if not why else "blocked: " + "; ".join(why))
        p = site_process()
        print(f"website: {p}")
        return 0 if not why else 1
    return restart(wait_min=args.wait)


if __name__ == "__main__":
    raise SystemExit(main())
