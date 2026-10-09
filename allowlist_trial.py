#!/usr/bin/env python3
"""allowlist_trial.py — read-only trial: what does the search allow-list change?

    python allowlist_trial.py trial  P58889A XH22398G V23409A CT23308A   # with vs without the allow-list
    python allowlist_trial.py retries X58848 P53971 P58889A V23410B V23385A ZT22917   # does a retry still fire?

Each car is aggregated once (the normal live scrape; the sticker, Carfax and recon
come from the caches) and its ad is generated in-process. NOTHING is saved or
sent: ad_history, the feature / trim-knowledge caches and email are all patched
out, and GENERATE_SEARCH_ALLOWLIST is only switched on for the duration of the
second run of a trial. Paid: one model call (plus its web searches) per run.

trial: the feature and trim-knowledge caches are bypassed so the research search
really runs; each car is generated twice, allow-list off then on, and the report
shows paragraph two of both, the research findings of both, and what is only in
one of them. retries: the normal configuration (caches as they are); reports
whether the retry guard fired and what it removed.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import sys
import time
from pathlib import Path
from unittest import mock

import adwriter as A
import aggregator as G


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", (text or "").replace("\n", " ")) if s.strip()]


def aggregate_for(stock: str) -> dict:
    hist = A.load_ad_history().get(stock) or {}
    return G.aggregate(stock, skip_recon=bool(hist.get("recon_pending")))


@contextlib.contextmanager
def hermetic(research_log: list[str], bypass_caches: bool):
    """No ad_history writes, no email, no cache writes; research findings are captured."""
    def boom(*a, **k):
        raise RuntimeError("trial must not save or send anything")

    patches = [
        mock.patch.object(A, "save_ad_history", boom),
        mock.patch.object(A, "_send_gmail", boom),
        mock.patch.object(A, "_cache_research_findings", lambda block, needs: research_log.append(block)),
        mock.patch.object(A, "_salvage_findings", lambda *a, **k: None),
        mock.patch.object(A, "save_feature", boom),
        mock.patch.object(A, "save_trim_knowledge", boom),
    ]
    if bypass_caches:
        patches += [mock.patch.object(A, "get_feature", lambda *a, **k: None),
                    mock.patch.object(A, "get_trim_knowledge", lambda *a, **k: None)]
    with contextlib.ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        yield


def generate(pkg: dict, *, allowlist: bool, bypass_caches: bool) -> dict:
    research: list[str] = []
    err = io.StringIO()
    started = time.monotonic()
    tool = {}
    real_tool = A.web_search_tool_for

    def spy(make):
        tool.update(real_tool(make))
        return tool

    with hermetic(research, bypass_caches), mock.patch.object(A, "GENERATE_SEARCH_ALLOWLIST", allowlist), \
            mock.patch.object(A, "web_search_tool_for", spy), contextlib.redirect_stderr(err):
        try:
            ad, feedback = A._generate_from_package(copy.deepcopy(pkg))
            error = None
        except Exception as exc:  # noqa: BLE001
            ad, feedback, error = "", None, f"{type(exc).__name__}: {exc}"
    paras = A.split_ad_paragraphs(ad)
    log = err.getvalue()
    return {
        "allowlist": allowlist, "seconds": round(time.monotonic() - started), "error": error,
        "paragraph_two": paras.get("paragraph_two", ""), "ad": ad,
        "allowed_domains": tool.get("allowed_domains"), "search_used": bool(tool),
        "research": "\n".join(research), "log": log,
        "retried": [ln for ln in log.splitlines() if "retrying once" in ln],
        "removed": [ln for ln in log.splitlines() if "removed sentence" in ln or "removed after retry" in ln],
    }


def research_lines(block: str) -> dict[str, str]:
    out = {}
    for ln in block.splitlines():
        parts = [p.strip() for p in ln.split("::")]
        if len(parts) >= 2 and parts[0]:
            out[parts[0]] = parts[1] + (f"  <{parts[-1]}>" if len(parts) >= 3 else "")
    return out


def run_trial(stocks: list[str]) -> list[dict]:
    results = []
    for s in stocks:
        print(f"[trial] {s}: aggregating ...", flush=True)
        pkg = aggregate_for(s)
        if pkg.get("reason") == "incomplete_data":
            results.append({"stock": s, "skipped": pkg.get("message")})
            continue
        runs = []
        for allow in (False, True):
            print(f"[trial] {s}: generating, allow-list {'ON' if allow else 'off'} ...", flush=True)
            runs.append(generate(pkg, allowlist=allow, bypass_caches=True))
        results.append({"stock": s, "vehicle": (pkg.get("vehicle") or {}).get("year_make_model"),
                        "status": (pkg.get("vehicle") or {}).get("status_code"), "runs": runs})
    return results


def run_retries(stocks: list[str]) -> list[dict]:
    results = []
    for s in stocks:
        print(f"[retries] {s}: aggregating ...", flush=True)
        pkg = aggregate_for(s)
        if pkg.get("reason") == "incomplete_data":
            results.append({"stock": s, "skipped": pkg.get("message")})
            continue
        print(f"[retries] {s}: generating ...", flush=True)
        results.append({"stock": s, **generate(pkg, allowlist=False, bypass_caches=False)})
    return results


def main(argv: list[str]) -> int:
    import api_cost
    import log_stamp

    log_stamp.install()
    api_cost.use_dev_key()    # an ad-hoc trial: the dev key when credentials.py has one
    if len(argv) < 2 or argv[0] not in ("trial", "retries"):
        print(__doc__)
        return 2
    mode, stocks = argv[0], [a.upper() for a in argv[1:]]
    data = run_trial(stocks) if mode == "trial" else run_retries(stocks)
    out = Path(__file__).with_name(f"allowlist_{mode}_results.json")
    out.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    print(f"[{mode}] results -> {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
