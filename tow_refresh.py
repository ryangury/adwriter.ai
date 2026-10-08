#!/usr/bin/env python3
"""tow_refresh.py — bring the stored ads' towing figures in line with verified ratings.

    python tow_refresh.py                     # dry run (default): what would change, nothing written
    python tow_refresh.py --lookup            # also run the paid lookups still missing (count and
                                              # expected cost are printed first), then the dry run
    python tow_refresh.py --apply --group overstated,unverifiable [--only P25418,...]
                                              # --apply needs --only and/or --group; it backs up
                                              # ad_history.json first and refuses while
                                              # orchestrator.lock exists
    groups: overstated, understated, unverifiable, matches, gaps

For every LIVE ad (in the inventory snapshot, not flagged absent) that states a
towing figure, and every live ad whose sticker / options say it can tow but whose
text states none: the stated figure and where it came from, the verified figure
for the exact configuration (towing.py: a tow_overrides.json entry, else a
manufacturer page for year / make / model / engine-or-EV-trim / drivetrain /
cab and bed / printed tow equipment), and the action:

  keep     the stated figure is the verified one
  replace  stated figure differs (overstated / understated): the sentence(s)
           stating it become TOWING_SENTENCE
  remove   no verified figure (unverifiable): the sentence(s) stating it go
  add      a gap with a verified figure: TOWING_SENTENCE goes in after the
           engine sentence

Edits are deterministic (no model call). Only ads whose text changes get their
removed sentences appended to stale_phrases and their verification fields
cleared (the verifier re-checks; the Ad Posting Alert lists them to repost).
A stated figure equal to a round kilogram value converted to pounds (7,716 lbs
= 3,500 kg) is flagged: that is a metric spec, rarely the US rating.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import adwriter as A
import towing as T
from powertrain import cached_texts, classify
from run_lock import ORCHESTRATOR_LOCK_PATH
from vehicle_cache import get_window_sticker

HERE = Path(__file__).resolve().parent
SNAPSHOT_PATH = HERE / "last_inventory_snapshot.json"
VERIFICATION_FIELDS = ("verification_verdict", "match_score", "last_verified", "price_mismatch",
                       "identity_confirmed", "verification_note")
GROUPS = ("overstated", "understated", "unverifiable", "matches", "gaps")

# One lookup = up to TOW_LOOKUP_MAX_USES web searches ($10 / 1,000) plus the
# search results read as input tokens and a short reply on the lookup model.
LOOKUP_COST_TYPICAL = 0.15
LOOKUP_COST_RANGE = (0.08, 0.30)

KG_PER_LB = 2.20462


def kg_conversion(lbs: int) -> int | None:
    """The round kilogram value (a multiple of 50 kg) that converts to `lbs`
    within one pound, or None."""
    for kg in range(500, 8001, 50):
        if abs(kg * KG_PER_LB - lbs) <= 1.0:
            return kg
    return None


def _old_cache_rows() -> list[dict]:
    """towing_capacity rows from the newest feature_cache.db backup that still
    has them (the table was emptied on 2026-10-05): where a stated figure came
    from, for the report only."""
    for p in sorted(HERE.glob("feature_cache.db.backup-*"), reverse=True):
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute("SELECT * FROM towing_capacity")]
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        if rows:
            return rows
    return []


def _source_of(figs: list[int], ymm: str, feedback: str, old_rows: list[dict]) -> str:
    m = re.match(r"(\d{4})\s+(\S+)\s+(.*)", ymm or "")
    year, make, model = (int(m.group(1)), m.group(2).lower(), m.group(3).lower()) if m else (None, "", "")
    hits = [r for r in old_rows if r["year"] == year and r["make"].lower() == make
            and (r["model"].lower().startswith(model) or model in r["model"].lower()) and r["tow_rating_lbs"] in figs]
    if hits:
        return "; ".join(dict.fromkeys(f"old cache {r['tow_rating_lbs']:,} lbs <- {r['source_url']}" for r in hits))
    fb = " ".join(ln.strip() for ln in (feedback or "").splitlines() if re.search(r"\btow", ln, re.I))
    if fb:
        return "model's own search at write time — feedback: " + fb[:240]
    return "model's own search at write time (no record of the page)"


# Words that make a clause about towing (a clause with none of these, and no
# tow figure, is "other content").
_TOW_TOPIC_RE = re.compile(r"\btow(?:s|ed|ing)?\b|\btrailer(?:s|ing)?\b|\bhitch\b|\bpull(?:s|ing)?\b|\bhaul(?:s|ing)?\b", re.I)
# Where a trailing tow clause can be cut off: ", and <clause>", "; <clause>",
# " — <clause>", or a shared-subject ", and / and (is) rated|able|capable to tow".
_CLAUSE_SPLIT_RE = re.compile(r",\s+and\s+|;\s+|\s+[—–]\s+")
_TRAILING_TOW_RE = re.compile(r",?\s+and\s+(?:is\s+|are\s+|has\s+been\s+)?(?:rated|able|capable|certified)\b[^.]*\btow\b[^.]*$", re.I)


def strip_tow_clause(sentence: str) -> tuple[str | None, str]:
    """(the sentence without its tow figure, how). how is "whole" (the whole
    sentence is towing content: remove it), "clause" (only the trailing tow
    clause was cut; the rest is returned), or "manual" (no clean split: the
    caller skips the ad for a hand edit)."""
    s = sentence.strip()
    body = s.rstrip(".!? ")
    m = _TRAILING_TOW_RE.search(body)
    if m and not T.tow_figures(body[: m.start()]):
        rest = body[: m.start()].rstrip(" ,")
        return (rest + "." if rest else None), ("clause" if rest else "whole")
    parts = _CLAUSE_SPLIT_RE.split(body)
    seps = _CLAUSE_SPLIT_RE.findall(body)
    if len(parts) == 1 or all(_TOW_TOPIC_RE.search(p) or T.tow_figures(p) for p in parts):
        return None, "whole"
    tow_idx = [i for i, p in enumerate(parts) if T.tow_figures(p)]
    if tow_idx == [len(parts) - 1]:
        kept = parts[0]
        for sep, p in zip(seps, parts[1:-1]):
            kept += sep + p
        kept = kept.rstrip(" ,")
        if T.tow_figures(kept) or not kept:
            return None, "manual"
        return kept + ".", "clause"
    return None, "manual"


def _tow_units(text: str) -> list[str]:
    return [s for s in A._split_sentences(text or "") if T.tow_figures(s)]


def _towing(stock: str, v: dict, allow_lookup: bool, refresh: bool = False) -> dict:
    vin = v.get("vin")
    sticker = (get_window_sticker(vin) or {}) if vin else {}
    raw = sticker.get("raw_text") or ""
    names = [o.get("name") or "" for o in (sticker.get("option_packages") or []) + (sticker.get("added_options_all") or [])]
    st, rc = cached_texts(vin) if vin else ("", "")
    pclass = classify(vin, v.get("year_make_model"), v.get("trim"), sticker_text=st, recon_text=rc)["class"]
    tow = T.towing_for(v.get("year_make_model"), v.get("trim"), v.get("body_style"), raw, names,
                       vin=vin, powertrain_class=pclass, allow_lookup=allow_lookup, force=True,
                       refresh=refresh)
    tow["sticker_triggers"] = T.triggered(raw, names)
    return tow


def _edit(entry: dict, action: str, sentence: str | None) -> tuple[dict, list[str]]:
    """(paragraphs after, removed sentences) for one ad. A sentence that is all
    towing goes whole; one that also carries other content loses only its
    trailing tow clause (strip_tow_clause). When no clean split exists the
    result carries "manual": True and nothing is changed for that ad."""
    keys = ("paragraph_one", "paragraph_two", "paragraph_three", "paragraph_four")
    before = {k: A._paragraph(entry, k) for k in keys}
    after = dict(before)
    removed: list[str] = []
    manual: list[str] = []
    if action in ("replace", "remove"):
        placed = False
        for k in keys:
            out = []
            for u in A._split_sentences(before[k]):
                if T.tow_figures(u):
                    rest, how = strip_tow_clause(u)
                    if how == "manual":
                        manual.append(u.strip())
                        out.append(u.strip())
                        continue
                    removed.append(u.strip())
                    if rest:
                        out.append(rest)
                    if action == "replace" and sentence and not placed:
                        out.append(sentence)
                        placed = True
                    continue
                out.append(u.strip())
            after[k] = " ".join(x for x in out if x)
        if manual:
            return {"before": before, "after": dict(before), "manual": manual}, []
    elif action == "add" and sentence:
        ad = "\n\n".join(before[k] for k in keys)
        new = A.insert_required_sentences(ad, {"towing_sentence": sentence}, ["towing_sentence"])
        after = A.split_ad_paragraphs(new)
    return {"before": before, "after": after, "manual": []}, removed


def plan(allow_lookup: bool) -> tuple[list[dict], list[dict]]:
    """(rows, configurations still needing a lookup)."""
    history = A.load_ad_history()
    snap = {v["stock_number"]: v for v in json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["vehicles"]}
    old_rows = _old_cache_rows()
    rows, pending = [], {}
    for stock, e in sorted(history.items()):
        v = snap.get(stock)
        if not v or e.get("absent_since") or not e.get("current_ad_text"):
            continue
        units = _tow_units(e["current_ad_text"])
        figs = sorted({f for u in units for f in T.tow_figures(u)})
        tow = _towing(stock, v, allow_lookup)
        if not figs and not tow["sticker_triggers"]:
            continue  # states nothing, can't tow per the sticker
        rating = tow.get("rating")
        if not rating and tow.get("note") == "not looked up yet":
            pending[T._key(tow["config"])] = tow["config_text"]
        # "Not looked up yet" is not "no match": those ads are left alone until
        # --lookup has run, so an apply never strips a figure a lookup would keep.
        unlooked = not rating and tow.get("note") == "not looked up yet"
        if figs:
            if rating and all(f == rating for f in figs):
                group, action = "matches", "keep"
            elif rating and max(figs) > rating:
                group, action = "overstated", "replace"
            elif rating:
                group, action = "understated", "replace"
            else:
                group, action = "unverifiable", ("pending lookup" if unlooked else "remove")
        else:
            group, action = "gaps", ("add" if rating else "pending lookup" if unlooked else "none")
        edit, removed = _edit(e, action, tow.get("sentence")) if action in ("replace", "remove", "add") else ({"before": {}, "after": {}, "manual": []}, [])
        if edit["manual"]:
            action = "hand edit"
        rows.append({
            "stock": stock, "ymm": v.get("year_make_model"), "trim": v.get("trim"), "status": v.get("status_code"),
            "figs": figs, "units": units, "source": _source_of(figs, v.get("year_make_model"), e.get("last_feedback"), old_rows) if figs else None,
            "kg": {f: kg_conversion(f) for f in figs if kg_conversion(f)},
            "rating": rating, "verified_by": (f"override ({tow['override'].get('source')}, {tow['override'].get('date')})" if tow.get("override")
                                             else tow.get("source_url")),
            "note": tow.get("note"), "config_text": tow["config_text"], "sentence": tow.get("sentence"),
            "group": group, "action": action, "removed": removed, **edit,
            "changes": bool(edit["after"]) and edit["after"] != edit["before"],
            "tow": tow,
        })
    return rows, [{"key": k, "config_text": t} for k, t in pending.items()]


def apply(rows: list[dict]) -> int:
    if ORCHESTRATOR_LOCK_PATH.exists():
        sys.exit("orchestrator.lock is present — a run may be in progress; not applying.")
    changing = [r for r in rows if r["changes"]]
    if not changing:
        print("nothing to apply")
        return 0
    backup = A.AD_HISTORY_PATH.with_name(f"ad_history.json.backup-{date.today().isoformat()}-tow-refresh")
    shutil.copy2(A.AD_HISTORY_PATH, backup)
    print(f"backed up ad_history.json -> {backup.name}")
    history = A.load_ad_history()
    for r in changing:
        e = history[r["stock"]]
        e.update(r["after"])
        e["current_ad_text"] = "\n\n".join(p for p in r["after"].values() if p)
        for f in VERIFICATION_FIELDS:
            e[f] = None
        stale = list(e.get("stale_phrases") or [])
        e["stale_phrases"] = stale + [s for s in r["removed"] if s not in stale]
        e["tow_refresh_date"] = date.today().isoformat()
        A.set_towing_review(e, r["tow"])
    A.save_ad_history(history)
    print(f"applied to {len(changing)} ads")
    return len(changing)


def _parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="default: show the plan, write nothing")
    mode.add_argument("--apply", action="store_true", help="write the selected ads (needs --only and/or --group)")
    ap.add_argument("--lookup", action="store_true", help="run the paid lookups still missing (count and cost printed first)")
    ap.add_argument("--lookup-stocks", default="",
                    help="STOCK[,...]: look up these inventory stocks' configurations, ad or not (cost printed first; "
                         "add --run to spend)")
    ap.add_argument("--relookup", default="", help="STOCK[,...]: with --lookup-stocks, ignore the cached result")
    ap.add_argument("--run", action="store_true", help="with --lookup-stocks: run the lookups")
    ap.add_argument("--detach", action="store_true",
                    help="with --run / --lookup: start the job detached (its own process, log in tow_logs/) and return")
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


MAX_LOOKUP_SPEND = 5.00


def lookup_stocks(stocks: list[str], relookup: set[str], run: bool) -> int:
    """Look up the tow configurations of specific inventory stocks, whether or
    not they have an ad now (a car deleted for a rebuild gets its rating cached
    before tomorrow's build). One lookup per configuration; cached ones are
    skipped unless in `relookup`. Prints the count and cost first and stops
    when the typical projection is over MAX_LOOKUP_SPEND."""
    snap = {v["stock_number"]: v for v in json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["vehicles"]}
    todo: dict[str, tuple[str, dict, bool]] = {}
    for s in stocks:
        v = snap.get(s)
        if not v:
            print(f"  {s}: not in the inventory snapshot - skipped")
            continue
        tow = _towing(s, v, allow_lookup=False)
        cfg = tow["config_text"]
        if (tow.get("config") or {}).get("missing"):
            print(f"  {s}: {tow['note']} - skipped")
            continue
        if s in relookup or tow.get("note") == "not looked up yet":
            todo.setdefault(cfg, (s, v, s in relookup))
        else:
            print(f"  {s}: {cfg} already cached ({tow.get('rating') or tow.get('note')}) - skipped")
    lo, hi = LOOKUP_COST_RANGE
    cost = len(todo) * LOOKUP_COST_TYPICAL
    print(f"lookups to run: {len(todo)} configuration(s), expected about ${cost:.2f} "
          f"(range ${len(todo) * lo:.2f}-${len(todo) * hi:.2f}); limits per configuration: streamed, aborted after "
          f"{T.TOW_SILENCE_S}s of silence, {T.TOW_CONFIG_BUDGET_S // 60}-minute budget, at most "
          f"{T.TOW_MAX_REQUESTS} billed requests")
    for cfg, (s, _v, re_) in todo.items():
        print(f"  - [{s}] {cfg}{'  (re-lookup: cached result ignored)' if re_ else ''}")
    if cost > MAX_LOOKUP_SPEND:
        print(f"STOP: projected ${cost:.2f} is over ${MAX_LOOKUP_SPEND:.2f}; nothing run")
        return 1
    if not run:
        print("(not run: add --run)")
        return 0
    import time as _t

    for i, (cfg, (s, v, re_)) in enumerate(todo.items(), 1):
        t0 = _t.monotonic()
        tow = _towing(s, v, allow_lookup=True, refresh=re_)
        print(f"  {i}/{len(todo)} [{s}] {cfg}: {format(tow['rating'], ',') + ' lbs' if tow.get('rating') else 'none'}"
              f" ({tow.get('source_url') or tow.get('note')}) in {_t.monotonic() - t0:.0f}s", flush=True)
    return 0


def detach(argv: list[str]) -> int:
    """Re-launch this script (without --detach) as a detached background job: no
    console, its own process group, output in tow_logs/. Returns at once."""
    import subprocess

    log_dir = Path(__file__).resolve().parent / "tow_logs"
    log_dir.mkdir(exist_ok=True)
    log = log_dir / f"tow_lookup_{datetime.now():%Y%m%d_%H%M%S}.log"
    rest = [a for a in argv if a != "--detach"]
    DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP = 0x00000008, 0x00000200
    with open(log, "ab") as out:
        proc = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), *rest], stdout=out, stderr=out,
                                stdin=subprocess.DEVNULL, close_fds=True,
                                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    print(f"detached tow lookup job: PID {proc.pid}, log {log}")
    return 0


def main(argv: list[str]) -> int:
    if "--detach" in argv:
        if not ({"--run", "--lookup"} & set(argv)):
            print("--detach needs --run (with --lookup-stocks) or --lookup; nothing started")
            return 2
        return detach(argv)
    args = _parse(argv)
    if args.lookup_stocks:
        return lookup_stocks([x.strip().upper() for x in args.lookup_stocks.split(",") if x.strip()],
                             {x.strip().upper() for x in args.relookup.split(",") if x.strip()}, args.run)
    rows, pending = plan(allow_lookup=False)
    lo, hi = LOOKUP_COST_RANGE
    print(f"TOW REFRESH — {'APPLY' if args.apply else 'DRY RUN (nothing written)'} — {date.today()}")
    print(f"lookups still needed: {len(pending)} configuration(s) — expected cost about "
          f"${len(pending) * LOOKUP_COST_TYPICAL:.2f} (range ${len(pending) * lo:.2f}-${len(pending) * hi:.2f}; "
          f"up to {T.TOW_LOOKUP_MAX_USES} web searches each)")
    for p in pending:
        print(f"  - {p['config_text']}")
    if args.lookup and pending:
        print(f"running {len(pending)} lookup(s) ...")
        rows, pending = plan(allow_lookup=True)
        print(f"lookups done; still unresolved: {len(pending)}")
    elif pending:
        print("  (not run: add --lookup to run them; until then those ads count as unverifiable / gaps)")

    selecting = bool(args.only or args.group)
    for r in rows:
        r["selected"] = (not selecting) or r["stock"] in args.only or r["group"] in args.group
    for g in GROUPS:
        grp = [r for r in rows if r["group"] == g and r["selected"]]
        if not grp:
            continue
        print(f"\n{'=' * 78}\n{g.upper()}: {len(grp)}\n{'=' * 78}")
        for r in grp:
            stated = ", ".join(f"{f:,} lbs" for f in r["figs"]) or "none"
            print(f"\n[{r['stock']}] {r['ymm']} ({r['trim']}, status {r['status']}) — action: {r['action'].upper()}")
            print(f"  stated   : {stated}" + (f"  <- {r['source']}" if r["source"] else ""))
            for f, kg in r["kg"].items():
                print(f"  KG FLAG  : {f:,} lbs = {kg:,} kg converted (metric spec, rarely the US rating)")
            print(f"  verified : {format(r['rating'], ',') + ' lbs' if r['rating'] else 'none'}"
                  + (f"  <- {r['verified_by']}" if r["rating"] else f"  ({r['note']})"))
            print(f"  config   : {r['config_text']}")
            for u in r["units"]:
                rest, how = strip_tow_clause(u)
                print(f"    - {u}")
                if how == "clause":
                    print(f"      (shares the sentence with other content: only the tow clause goes)\n    = {rest}")
                elif how == "manual":
                    print("      (no clean split: HAND EDIT — this ad is skipped by --apply)")
            if r["action"] in ("replace", "add") and r["sentence"]:
                print(f"    + {r['sentence']}")
    hand = [r for r in rows if r["action"] == "hand edit"]
    if hand:
        print(f"\n{'=' * 78}\nHAND EDIT ({len(hand)}): tow figure shares a sentence with other content and can't be cut cleanly")
        for r in hand:
            for u in r["manual"]:
                print(f"  [{r['stock']}] {u}")
    print(f"\n{'=' * 78}\nTOWING RATING NEEDS REVIEW (no verified figure; fill tow_overrides.json from the Database page):")
    for r in rows:
        if not r["rating"]:
            print(f"  [{r['stock']}] {r['ymm']} ({r['trim']}) — {r['note']}")
    changing = [r for r in rows if r["selected"] and r["changes"]]
    print(f"\nWOULD CHANGE ({len(changing)} selected ads; stale_phrases + verification fields only on these): "
          + ", ".join(r["stock"] for r in changing))
    if args.apply:
        apply([r for r in rows if r["selected"]])
    return 0


if __name__ == "__main__":
    import log_stamp

    log_stamp.install()  # timestamped lines: when each lookup finished
    raise SystemExit(main(sys.argv[1:]))
