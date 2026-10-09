#!/usr/bin/env python3
"""carfax_refresh.py — take unsupported Carfax claims out of the stored ads.

    python carfax_refresh.py                       # dry run (default): before / after, nothing written
    python carfax_refresh.py --apply --group accident,damage [--only P25418,...]
                                                   # --apply needs --only and/or --group; it backs up
                                                   # ad_history.json first and refuses while
                                                   # orchestrator.lock exists
    groups: accident, damage, unparsed, service

For every LIVE ad (in the inventory snapshot, not flagged absent) that claims a
clean history ("clean vehicle history", "clean Carfax", "no accidents", ...) or
all service at authorized dealers, the claim is checked against the stored
Carfax report's own text (carfax_history.py):

  accident   the report lists an "Accident reported" row
  damage     the report lists a "Damage reported" row or a damage badge (no accident)
  unparsed   the report's text can't be read (or there is none): no claim
  service    the history is clean, but the service records don't support
             "all service performed at authorized <make> dealers" (two records,
             a maintenance visit, all at the make's dealers)

The Python sentence ("Clean vehicle history, with all service ...") and the
model's paragraph-one variant ("..., personal use with clean vehicle history
confirmed by Carfax.") are rewritten deterministically (no model call). A
sentence with a claim in a shape the rewrite doesn't know is not touched: that
ad is listed for a hand edit and skipped by --apply. A following sentence that
may refer back to removed wording is flagged. Only ads whose text changes get
the removed sentences appended to stale_phrases and their verification fields
cleared (the verifier re-checks; the Ad Posting Alert lists them to repost).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

import adwriter as A
import carfax_history as H
from powertrain import split_ymm
from run_lock import ORCHESTRATOR_LOCK_PATH, lock_blocks_edit

HERE = Path(__file__).resolve().parent
SNAPSHOT_PATH = HERE / "last_inventory_snapshot.json"
VERIFICATION_FIELDS = A.VERIFICATION_FIELDS    # one list, defined where the text is stored
GROUPS = ("accident", "damage", "unparsed", "service")
KEYS = ("paragraph_one", "paragraph_two", "paragraph_three", "paragraph_four")
# Ads also changed by the 2026-10-05 warranty refresh: repost once, with both changes.
WARRANTY_REFRESH_OVERLAP = {"V23409A": "2026-10-05 warranty refresh (factory-warranty sentence added)",
                            "DT23368B": "2026-10-05 warranty refresh (factory-warranty sentence added)"}


def plan() -> list[dict]:
    history = A.load_ad_history()
    snap = {v["stock_number"]: v for v in json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["vehicles"]}
    rows = []
    for stock, e in sorted(history.items()):
        v = snap.get(stock)
        if not v or e.get("absent_since") or not e.get("current_ad_text"):
            continue
        text = e["current_ad_text"]
        claims_clean = bool(H.CLEAN_CLAIM_RE.search(text))
        claims_service = bool(H.SERVICE_CLAIM_RE.search(text))
        if not (claims_clean or claims_service):
            continue
        cf = A.carfax_verdict_for_stock(stock)["carfax"]
        h = H.carfax_history(cf.get("raw_text"), split_ymm(v.get("year_make_model"))[1])
        if claims_clean and not h["clean"]:
            group = ("unparsed" if not h["parsed"] else "accident" if h["accident_count"]
                     else "damage")
        elif claims_service and not h["service_ok"]:
            group = "service"
        else:
            group = None  # the claims hold
        before = {k: A._paragraph(e, k) for k in KEYS}
        after, removed, manual, dangling = dict(before), [], [], []
        if group:
            for k in KEYS:
                if before[k]:
                    r = H.scrub_claims(before[k], h["clean"], h["service_ok"])
                    after[k] = r["text"]
                    removed += r["removed"]
                    manual += r["manual"]
                    dangling += r["dangling"]
        rows.append({
            "stock": stock, "ymm": v.get("year_make_model"), "status": v.get("status_code"),
            "group": group, "history": h, "claims_clean": claims_clean, "claims_service": claims_service,
            "before": before, "after": after if not manual else dict(before),
            "removed": removed if not manual else [], "manual": manual, "dangling": dangling,
            "changes": bool(group) and not manual and after != before,
            "overlap": WARRANTY_REFRESH_OVERLAP.get(stock),
        })
    return rows


def apply(rows: list[dict]) -> int:
    holder = lock_blocks_edit(ORCHESTRATOR_LOCK_PATH)
    if holder is not None:
        sys.exit(f"orchestrator.lock is held by PID {holder} — a run may be in progress; not applying.")
    changing = [r for r in rows if r["changes"]]
    if not changing:
        print("nothing to apply")
        return 0
    backup = A.AD_HISTORY_PATH.with_name(f"ad_history.json.backup-{date.today().isoformat()}-carfax-refresh")
    shutil.copy2(A.AD_HISTORY_PATH, backup)
    print(f"backed up ad_history.json -> {backup.name}")
    history = A.load_ad_history()
    for r in changing:
        e = history[r["stock"]]
        e.update(r["after"])
        A.set_current_ad_text(e, "\n\n".join(r["after"][k] for k in KEYS if r["after"][k]))
        A.clear_verification(e)    # the edit changed the text even if the join came out equal
        stale = list(e.get("stale_phrases") or [])
        e["stale_phrases"] = stale + [s for s in r["removed"] if s not in stale]
        e["carfax_refresh_date"] = date.today().isoformat()
    A.save_ad_history(history)
    print(f"applied to {len(changing)} ads")
    return len(changing)


def _parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="default: show before / after, write nothing")
    mode.add_argument("--apply", action="store_true", help="write the selected ads (needs --only and/or --group)")
    ap.add_argument("--only", default="", help="STOCK[,STOCK...]")
    ap.add_argument("--group", default="", help="comma list of: " + ", ".join(GROUPS))
    args = ap.parse_args(argv)
    args.only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    args.group = {x.strip().lower() for x in args.group.split(",") if x.strip()}
    bad = args.group - set(GROUPS)
    if bad:
        ap.error(f"unknown --group {', '.join(sorted(bad))}; choose from {', '.join(GROUPS)}")
    if args.apply and not (args.only or args.group):
        ap.error("--apply needs a selector: --only STOCK[,STOCK...] and/or --group {" + ",".join(GROUPS) + "}")
    return args


def main(argv: list[str]) -> int:
    args = _parse(argv)
    rows = plan()
    selecting = bool(args.only or args.group)
    for r in rows:
        r["selected"] = bool(r["group"]) and ((not selecting) or r["stock"] in args.only or r["group"] in args.group)
    print(f"CARFAX REFRESH — {'APPLY' if args.apply else 'DRY RUN (nothing written)'} — {date.today()}")
    print(f"live ads with a clean-history or all-service claim: {len(rows)}; "
          f"claims hold: {sum(1 for r in rows if not r['group'])}; unsupported: {sum(1 for r in rows if r['group'])}")
    for g in GROUPS:
        grp = [r for r in rows if r["group"] == g and (r["selected"] or not selecting)]
        if not grp:
            continue
        print(f"\n{'=' * 78}\n{g.upper()}: {len(grp)}\n{'=' * 78}")
        for r in grp:
            h = r["history"]
            print(f"\n[{r['stock']}] {r['ymm']} (status {r['status']})"
                  + (f"  ** ALSO IN THE {r['overlap'].upper()} — repost once with both changes **" if r["overlap"] else ""))
            for e in h["events"]:
                print(f"  carfax : {e['kind']} reported {e['date']}" + (f", {e['severity']} damage" if e["severity"] else "")
                      + (f" — {e['detail']}" if e["detail"] else ""))
            if g in ("unparsed", "damage", "accident") and not h["events"]:
                print("  carfax : " + "; ".join(h["reasons"]))
            if g == "service" or (r["claims_service"] and not h["service_ok"]):
                print("  service: " + "; ".join(h["service_reasons"]))
            for k in KEYS:
                if r["before"][k] != r["after"][k]:
                    print(f"  {k}:\n    - {r['before'][k]}\n    + {r['after'][k]}")
            for s in r["removed"]:
                print(f"  stale_phrase: {s}")
            for s in r["dangling"]:
                print(f"  CHECK: may refer back to removed wording: {s}")
            for s in r["manual"]:
                print(f"  HAND EDIT (claim in a shape the rewrite doesn't know; ad skipped): {s}")
    hand = [r for r in rows if r["manual"]]
    if hand:
        print(f"\n{'=' * 78}\nHAND EDIT: {len(hand)} — " + ", ".join(r["stock"] for r in hand))
    changing = [r for r in rows if r["selected"] and r["changes"]]
    print(f"\nREPOST SET ({len(changing)} ads whose text changes; stale_phrases + verification fields only on these):")
    print("  " + ", ".join(r["stock"] + (" (+warranty refresh)" if r["overlap"] else "") for r in changing))
    if args.apply:
        apply([r for r in rows if r["selected"]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
