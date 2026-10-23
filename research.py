#!/usr/bin/env python3
"""research.py — electric ranges and tow ratings a person looked up, keyed by configuration.

Production builds make no live search for a range or a tow rating. Where one is needed:

    range   powertrain class override (VIN) > a researched figure for the configuration (this module)
            > the range cache > pass 0 (fueleconomy.gov, free) > RESEARCH NEEDED
    tow     tow override (VIN, tow_overrides.json) > a researched figure for the configuration (this module)
            > the tow cache (towing_config) > RESEARCH NEEDED

A miss is listed under RESEARCH NEEDED in the Build Summary and on the site's Research page (/research), with the
configuration, what is missing, a reason code and the candidates found so far (figure, source, quote). A person
picks a candidate or types a figure, with its source URL and the quote that states it; that fact is used by every
car of the same configuration from then on. "Refresh text" puts the sentence into a stored ad with the existing
no-model edit (tow_refresh's deterministic sentence edit; the range sentence the same way).

Tables (feature_cache.db):
    config_fact       one researched figure per (kind, configuration): figure, source_url, quote, entered_date
    config_candidate  figures seen for a configuration (pass 0's other records, cached lookups, the prototype)

Configuration key: year, make, model, engine, drivetrain, cab, bed, tow package (for a range the tow package is
always '': it does not change a range). The engine is the sticker's engine line (towing.vehicle_config); a
battery-electric car's is "battery-electric <trim>"; a plug-in hybrid with no engine line on file uses
"trim <trim>" so its range can still be keyed.
"""
from __future__ import annotations

import contextlib
import json
import re
import shutil
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import feature_cache

KINDS = ("range", "tow")
KEY_COLS = ("year", "make", "model", "engine", "drivetrain", "cab", "bed", "tow_package")
LABELS = {"range": "electric range", "tow": "tow rating"}
UNITS = {"range": "miles", "tow": "lbs"}
LIMITS = {"range": (5, 700), "tow": (500, 40000)}

# Reason codes for a missing figure.
REASONS = {
    "epa_no_entry": "fueleconomy.gov has no entry for this model year and trim",
    "epa_ambiguous": "fueleconomy.gov has more than one candidate, or records that disagree",
    "epa_unavailable": "fueleconomy.gov could not be reached this run",
    "no_year_make_model": "no year / make / model on the record",
    "config_incomplete": "the configuration could not be read from the sticker / record",
    "not_looked_up": "no figure on file for this configuration (a build makes no live lookup)",
    "not_entered": "a lookup found a figure; a tow figure is stated only once it is entered here (or a VIN override)",
    "no_exact_match": "a lookup found no rating for this exact configuration",
    "lookup_failed": "the lookup failed",
    "not_checked": "not looked up yet; the next build runs pass 0 (fueleconomy.gov, free)",
    "other": "see the detail",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_fact (
    kind          TEXT NOT NULL,
    year          INTEGER NOT NULL,
    make          TEXT NOT NULL COLLATE NOCASE,
    model         TEXT NOT NULL COLLATE NOCASE,
    engine        TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    drivetrain    TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    cab           TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    bed           TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    tow_package   TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    figure        INTEGER NOT NULL,
    source_url    TEXT NOT NULL,
    quote         TEXT NOT NULL,
    entered_date  TEXT NOT NULL,
    UNIQUE (kind, year, make, model, engine, drivetrain, cab, bed, tow_package)
);
CREATE TABLE IF NOT EXISTS config_candidate (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    year          INTEGER NOT NULL,
    make          TEXT NOT NULL COLLATE NOCASE,
    model         TEXT NOT NULL COLLATE NOCASE,
    engine        TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    drivetrain    TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    cab           TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    bed           TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    tow_package   TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    figure        INTEGER NOT NULL,
    source_url    TEXT NOT NULL DEFAULT '',
    quote         TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL DEFAULT '',
    found_date    TEXT NOT NULL,
    UNIQUE (kind, year, make, model, engine, drivetrain, cab, bed, tow_package, figure, source_url)
);
"""


@contextlib.contextmanager
def _connect():
    conn = sqlite3.connect(str(feature_cache.DB_PATH))       # read at call time (the tests point it at a temp file)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# the configuration key
# --------------------------------------------------------------------------- #
def key_of(kind: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The key columns for one kind, or None when the year / make / model / engine is missing."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    k = {c: (cfg.get(c) or "") for c in KEY_COLS}
    k["year"] = cfg.get("year")
    if kind == "range":
        k["tow_package"] = ""
    if not (k["year"] and k["make"] and k["model"] and k["engine"]):
        return None
    k["year"] = int(k["year"])
    return k


def key_id(kind: str, key: dict[str, Any]) -> str:
    """A stable text id for a key (the site's form field)."""
    return json.dumps([kind] + [key[c] for c in KEY_COLS])


def key_from_id(text: str) -> tuple[str, dict[str, Any]]:
    parts = json.loads(text)
    kind, vals = parts[0], parts[1:]
    if kind not in KINDS or len(vals) != len(KEY_COLS):
        raise ValueError("not a configuration id")
    key = dict(zip(KEY_COLS, vals))
    key["year"] = int(key["year"])
    return kind, key


def describe(kind: str, key: dict[str, Any]) -> str:
    parts = [str(key["year"]), key["make"], key["model"], key["engine"], key["drivetrain"] or "drivetrain ?"]
    if key.get("cab") or key.get("bed"):
        parts += [f"{key.get('cab') or '?'} cab", f"{key.get('bed') or '?'} bed"]
    if kind == "tow":
        parts.append(f"[tow equipment: {key.get('tow_package') or 'none printed'}]")
    return " ".join(p for p in parts if p)


def range_config(year_make_model: str | None, trim: str | None, body_style: str | None, sticker_text: str | None,
                 powertrain_class: str | None) -> dict[str, Any]:
    """The configuration a range is keyed by (towing.vehicle_config, tow package dropped; a plug-in hybrid with no
    engine line on file is keyed by its trim)."""
    import towing

    cfg = towing.vehicle_config(year_make_model, trim, body_style, sticker_text, powertrain_class)
    cfg = dict(cfg, tow_package="")
    if not cfg.get("engine") and trim:
        cfg["engine"] = f"trim {' '.join(trim.split())}"
    return cfg


# --------------------------------------------------------------------------- #
# facts and candidates
# --------------------------------------------------------------------------- #
def _where(key: dict[str, Any]) -> tuple[str, tuple]:
    return " AND ".join(f"{c}=?" for c in KEY_COLS), tuple(key[c] for c in KEY_COLS)


def get_fact(kind: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    key = key_of(kind, cfg)
    if not key:
        return None
    where, vals = _where(key)
    with _connect() as conn:
        row = conn.execute(f"SELECT * FROM config_fact WHERE kind=? AND {where}", (kind,) + vals).fetchone()
    return dict(row) if row else None


def validate(kind: str, figure: Any, source_url: str | None, quote: str | None) -> tuple[int, str, str]:
    """(figure, source_url, quote) or ValueError: a whole number in range, a URL, and the quote that states it."""
    raw = str(figure if figure is not None else "").replace(",", "").replace(UNITS[kind], "").strip()
    if not raw.isdigit():
        raise ValueError(f"the {LABELS[kind]} must be a whole number of {UNITS[kind]}, not {figure!r}")
    n = int(raw)
    lo, hi = LIMITS[kind]
    if not lo <= n <= hi:
        raise ValueError(f"{n:,} {UNITS[kind]} is out of range for a {LABELS[kind]} ({lo:,}-{hi:,})")
    url = (source_url or "").strip()
    if not re.match(r"https?://\S+$", url):
        raise ValueError("give the source page's URL (http... )")
    q = " ".join((quote or "").split())
    if not q:
        raise ValueError("paste the sentence or table row that states the figure (quote)")
    return n, url, q


def set_fact(kind: str, cfg: dict[str, Any], figure: Any, source_url: str | None, quote: str | None) -> dict[str, Any]:
    key = key_of(kind, cfg)
    if not key:
        raise ValueError("the configuration is incomplete (year, make, model and engine are needed)")
    n, url, q = validate(kind, figure, source_url, quote)
    row = dict(key, kind=kind, figure=n, source_url=url, quote=q, entered_date=date.today().isoformat())
    cols = ("kind",) + KEY_COLS + ("figure", "source_url", "quote", "entered_date")
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO config_fact ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT (kind, {', '.join(KEY_COLS)}) DO UPDATE SET figure=excluded.figure, "
            "source_url=excluded.source_url, quote=excluded.quote, entered_date=excluded.entered_date",
            tuple(row[c] for c in cols))
    return row


def clear_fact(kind: str, cfg: dict[str, Any]) -> bool:
    key = key_of(kind, cfg)
    if not key:
        return False
    where, vals = _where(key)
    with _connect() as conn:
        return conn.execute(f"DELETE FROM config_fact WHERE kind=? AND {where}", (kind,) + vals).rowcount > 0


def all_facts() -> list[dict[str, Any]]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM config_fact ORDER BY kind, make, model, year")]


def add_candidates(kind: str, cfg: dict[str, Any], items: list[dict[str, Any]], origin: str) -> int:
    """Store figures seen for a configuration ({figure, url, quote | label}); duplicates are ignored."""
    key = key_of(kind, cfg)
    if not key or not items:
        return 0
    cols = ("kind",) + KEY_COLS + ("figure", "source_url", "quote", "origin", "found_date")
    n = 0
    with _connect() as conn:
        for it in items:
            try:
                fig = int(it.get("figure"))
            except (TypeError, ValueError):
                continue
            row = dict(key, kind=kind, figure=fig, source_url=it.get("url") or "",
                       quote=" ".join(str(it.get("quote") or it.get("label") or "").split())[:500],
                       origin=it.get("origin") or origin, found_date=date.today().isoformat())
            n += conn.execute(f"INSERT OR IGNORE INTO config_candidate ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                              tuple(row[c] for c in cols)).rowcount
    return n


def candidates(kind: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Stored candidates for the configuration, plus (tow) cached ratings of other configurations of the same
    year / make / model, labelled as such."""
    key = key_of(kind, cfg)
    if not key:
        return []
    where, vals = _where(key)
    with _connect() as conn:
        out = [dict(r) for r in conn.execute(
            f"SELECT figure, source_url, quote, origin, found_date FROM config_candidate WHERE kind=? AND {where} "
            "ORDER BY found_date DESC, figure", (kind,) + vals)]
        if kind == "tow":
            with contextlib.suppress(sqlite3.Error):
                for r in conn.execute(
                        "SELECT * FROM towing_config WHERE year=? AND make=? AND model=? AND tow_rating_lbs IS NOT NULL",
                        (key["year"], key["make"], key["model"])):
                    same = all((r[c] or "").lower() == (key[c] or "").lower() for c in ("engine", "drivetrain", "cab", "bed", "tow_package"))
                    out.append({"figure": r["tow_rating_lbs"], "source_url": r["source_url"] or "",
                                "quote": r["page_config"] or "", "found_date": r["checked_date"],
                                "origin": "tow cache (this configuration)" if same else
                                f"tow cache, OTHER configuration: {r['engine']} {r['drivetrain']} {r['cab']} {r['bed']} [{r['tow_package'] or 'no package'}]"})
    return out


# --------------------------------------------------------------------------- #
# what a built ad is missing
# --------------------------------------------------------------------------- #
def range_reason(flag_or_note: str | None, status: str | None = None) -> str:
    t = (flag_or_note or "").lower()
    if status == "ambiguous" or "ambiguous" in t or "conflict" in t:
        return "epa_ambiguous"
    if "this run" in t or "unreachable" in t or "could not be reached" in t:
        return "epa_unavailable"
    if "no year/make/model" in t:
        return "no_year_make_model"
    if "has no" in t or "no entry" in t or status == "none":
        return "epa_no_entry"
    return "other"


def tow_reason(note: str | None) -> str:
    t = (note or "").lower()
    if "not used until it is entered" in t or "not from an override or the research page" in t:
        return "not_entered"
    if "not looked up" in t:
        return "not_looked_up"
    if "incomplete" in t:
        return "config_incomplete"
    if "failed" in t or "stopped" in t or "skipped" in t:
        return "lookup_failed"
    if "no exact match" in t or "no rating" in t or "none" in t or t:
        return "no_exact_match"
    return "other"


def items_for(pt: dict[str, Any] | None, tow: dict[str, Any] | None) -> list[dict[str, Any]]:
    """RESEARCH NEEDED items for one car from its powertrain (vehicle_powertrain) and towing (towing_for) results."""
    out: list[dict[str, Any]] = []
    pt = pt or {}
    rng = pt.get("range") or {}
    from powertrain import PLUG_IN

    if pt.get("class") in PLUG_IN and not rng.get("miles"):
        cfg = rng.get("config") or {}
        key = key_of("range", cfg) if cfg else None
        out.append({
            "kind": "range", "missing": LABELS["range"], "reason": rng.get("reason") or range_reason(rng.get("flag")),
            "detail": rng.get("flag") or "", "config_text": describe("range", key) if key else "configuration incomplete",
            "key_id": key_id("range", key) if key else None,
            "candidates": [_cand(c) for c in (candidates("range", cfg) if key else [])],
        })
    tow = tow or {}
    if tow.get("triggered") and not tow.get("rating"):
        cfg = tow.get("config") or {}
        key = key_of("tow", cfg) if cfg else None
        out.append({
            "kind": "tow", "missing": LABELS["tow"], "reason": tow_reason(tow.get("note")), "detail": tow.get("note") or "",
            "config_text": tow.get("config_text") or (describe("tow", key) if key else "configuration incomplete"),
            "key_id": key_id("tow", key) if key else None,
            "candidates": [_cand(c) for c in (candidates("tow", cfg) if key else [])],
        })
    return out


def _cand(c: dict[str, Any]) -> dict[str, Any]:
    return {k: c.get(k) for k in ("figure", "source_url", "quote", "origin")}


def set_research_needed(entry: dict[str, Any], pt: dict[str, Any] | None, tow: dict[str, Any] | None) -> None:
    """ad_history's research_needed: what the ad could not state and why (removed once nothing is missing)."""
    items = items_for(pt, tow)
    if items:
        entry["research_needed"] = items
    else:
        entry.pop("research_needed", None)


def summary_lines(rows: list[dict[str, Any]], *, max_candidates: int = 4) -> list[str]:
    """The Build Summary's RESEARCH NEEDED section body: rows of {stock, ymm, items}."""
    out = []
    for r in rows:
        for it in r["items"]:
            out.append(f"  [{r['stock']}]  {r.get('ymm') or 'unknown'}  —  {it['missing']} missing ({it['reason']})")
            out.append(f"      configuration: {it['config_text']}")
            if it.get("detail"):
                out.append(f"      why: {it['detail'][:220]}")
            cands = it.get("candidates") or []
            if cands:
                for c in cands[:max_candidates]:
                    q = f" — \"{c['quote'][:120]}\"" if c.get("quote") else ""
                    out.append(f"      candidate: {c['figure']:,} {UNITS[it['kind']]} ({c.get('origin') or 'seen'}) {c.get('source_url') or ''}{q}")
                if len(cands) > max_candidates:
                    out.append(f"      ... {len(cands) - max_candidates} more on the Research page")
            else:
                out.append("      candidates: none found yet")
    return out


# --------------------------------------------------------------------------- #
# "Refresh text": the no-model edit of one stored ad
# --------------------------------------------------------------------------- #
def range_sentence(miles: int, source: str | None) -> str:
    wording = {"manufacturer": "manufacturer-estimated", "manual": "estimated"}.get(source or "", "EPA-estimated")
    return f"It offers up to {miles} miles of {wording} electric range."


def _vehicle(stock: str) -> dict[str, Any]:
    import adwriter as A

    v = A._snapshot_vehicle(stock)
    if not v:
        raise LookupError(f"{stock} is not in the inventory snapshot")
    return v


def plan_refresh(stock: str) -> dict[str, Any]:
    """What 'Refresh text' would do to one stored ad (nothing written): the tow edit (tow_refresh's deterministic
    edit: replace a differing tow figure or add TOWING_SENTENCE after the engine sentence) and the range edit
    (a range claim that differs is replaced by the range sentence, else the sentence is added after the engine
    sentence). With no figure for a kind, that kind is left alone."""
    import adwriter as A
    import tow_refresh as TR
    import towing as T
    from powertrain import range_claim_numbers, vehicle_powertrain

    history = A.load_ad_history()
    entry = history.get(stock)
    if not entry or not entry.get("current_ad_text"):
        raise LookupError(f"{stock} has no stored ad")
    v = _vehicle(stock)
    keys = ("paragraph_one", "paragraph_two", "paragraph_three", "paragraph_four")
    before = {k: A._paragraph(entry, k) for k in keys}
    after = dict(before)
    removed: list[str] = []
    notes: list[str] = []

    tow = TR._towing(stock, v, allow_lookup=False)
    rating = tow.get("rating")
    if rating:
        figs = sorted({f for u in TR._tow_units(entry["current_ad_text"]) for f in T.tow_figures(u)})
        action = "keep" if figs and all(f == rating for f in figs) else ("replace" if figs else "add")
        if action != "keep":
            edit, rem = TR._edit({**entry, **after}, action, tow.get("sentence"))
            if edit["manual"]:
                notes.append(f"tow: no clean edit, hand edit needed ({'; '.join(edit['manual'])[:160]})")
            else:
                after, removed = dict(edit["after"]), removed + rem
                notes.append(f"tow: {action} -> {tow['sentence']}")
        else:
            notes.append(f"tow: already states {rating:,} lbs")
    else:
        notes.append("tow: no rating on file, left alone")

    pt = vehicle_powertrain(v.get("vin"), v.get("year_make_model"), v.get("trim"), body_style=v.get("body_style"))
    rng = pt.get("range") or {}
    if rng.get("miles"):
        sentence = range_sentence(rng["miles"], rng.get("source"))
        stated = [(k, u) for k in keys for u in A._split_sentences(after[k]) if range_claim_numbers(u)]
        wrong = [(k, u) for k, u in stated if any(abs(n - rng["miles"]) > 1 for n in range_claim_numbers(u))]
        if stated and not wrong:
            notes.append(f"range: already states {rng['miles']} miles")
        elif wrong:
            # The first differing sentence becomes the range sentence; any other differing one goes.
            bad = {u.strip() for _, u in wrong}
            placed = False
            for k in keys:
                out = []
                for x in (u.strip() for u in A._split_sentences(after[k])):
                    if x in bad:
                        removed.append(x)
                        if not placed:
                            out.append(sentence)
                            placed = True
                        continue
                    out.append(x)
                after[k] = " ".join(x for x in out if x)
            notes.append(f"range: replace -> {sentence}")
        else:
            ad = "\n\n".join(after[k] for k in keys)
            new = A.insert_required_sentences(ad, {"towing_sentence": sentence}, ["towing_sentence"])
            after = A.split_ad_paragraphs(new)
            notes.append(f"range: add -> {sentence}")
    elif pt.get("class") in ("phev", "bev"):
        notes.append("range: no range on file, left alone")
    return {"stock": stock, "before": before, "after": after, "removed": removed, "notes": notes,
            "changes": after != before, "tow": tow, "pt": pt}


def apply_refresh(stock: str) -> dict[str, Any]:
    """Write plan_refresh's edit: back up ad_history.json (timestamped), store the text, clear the verification
    fields (the verifier re-checks), add removed sentences to stale_phrases, update towing_review and
    research_needed. Refuses while orchestrator.lock exists."""
    import adwriter as A
    from run_lock import ORCHESTRATOR_LOCK_PATH

    if ORCHESTRATOR_LOCK_PATH.exists():
        raise RuntimeError("orchestrator.lock is present — a run may be in progress; not editing ads now")
    plan = plan_refresh(stock)
    if not plan["changes"]:
        return dict(plan, applied=False)
    backup = A.AD_HISTORY_PATH.with_name(f"ad_history.json.backup-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}-research-{stock}")
    shutil.copy2(A.AD_HISTORY_PATH, backup)
    history = A.load_ad_history()
    e = history[stock]
    old_text = e.get("current_ad_text")
    e.update(plan["after"])
    A.set_current_ad_text(e, "\n\n".join(p for p in plan["after"].values() if p))
    A.note_expected_phrases(e, old_text)
    A.clear_verification(e)
    stale = list(e.get("stale_phrases") or [])
    e["stale_phrases"] = stale + [s for s in plan["removed"] if s not in stale]
    e["research_refresh_date"] = date.today().isoformat()
    A.set_towing_review(e, plan["tow"])
    set_research_needed(e, plan["pt"], plan["tow"])
    A.save_ad_history(history)
    print(f"[research] {stock}: text refreshed ({'; '.join(plan['notes'])}); backup {backup.name}", file=sys.stderr)
    return dict(plan, applied=True, backup=backup.name)


# --------------------------------------------------------------------------- #
# The Research page's list, computed at page load (read-only)
# --------------------------------------------------------------------------- #
def live_items(vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{stock, ymm, items}] for the cars in the inventory snapshot that miss an electric range or a tow rating,
    from the caches only: no search, no pass 0 call, no write (ad_history and the caches are only read).
    A plug-in whose range was never looked up shows reason "not_checked" (the next build runs pass 0)."""
    from powertrain import PLUG_IN, _range_info, cached_texts, classify, split_ymm
    from towing import towing_for
    from vehicle_cache import get_window_sticker

    out = []
    for v in vehicles:
        stock, vin = v.get("stock_number"), v.get("vin")
        ymm, trim, body = v.get("year_make_model"), v.get("trim"), v.get("body_style")
        try:
            st, rc = cached_texts(vin) if vin else ("", "")
            cls = classify(vin, ymm, trim, sticker_text=st, recon_text=rc)["class"]
            pt = {"class": cls, "range": {}}
            if cls in PLUG_IN:
                cfg = range_config(ymm, trim, body, st, cls)
                fact = get_fact("range", cfg)
                year, make, model = split_ymm(ymm)
                row = feature_cache.get_electric_range(year, make, model, trim) if (year and make and model) else None
                if fact or (row and row.get("powertrain") == cls and row.get("range_miles")):
                    rng = {"miles": (fact or {}).get("figure") or row["range_miles"]}
                elif row and row.get("powertrain") == cls:
                    rng = _range_info(None, None, None, None, f"electric range omitted: {row.get('note')}", True,
                                      reason=range_reason(row.get("note"), "ambiguous" if row.get("source") in ("ambiguous", "conflict") else None))
                else:
                    rng = _range_info(None, None, None, None, "not looked up yet (the next build runs pass 0)", False,
                                      reason="not_checked")
                rng["config"] = cfg
                pt["range"] = rng
            sticker = (get_window_sticker(vin) or {}) if vin else {}
            names = [o.get("name") or "" for o in (sticker.get("option_packages") or []) + (sticker.get("added_options_all") or [])]
            tow = towing_for(ymm, trim, body, sticker.get("raw_text") or "", names, vin=vin, powertrain_class=cls,
                             allow_lookup=False)
            items = items_for(pt, tow)
        except Exception as exc:  # noqa: BLE001 - one car must not break the page
            items = [{"kind": "?", "missing": "range / tow", "reason": "other", "detail": f"could not be checked ({exc})",
                      "config_text": ymm or "?", "key_id": None, "candidates": []}]
        if items:
            out.append({"stock": stock, "ymm": ymm, "items": items})
    return out


# --------------------------------------------------------------------------- #
# "Find candidates": one search, on a click only. Never accepted automatically.
# --------------------------------------------------------------------------- #
FIND_MODEL = "claude-haiku-4-5"
FIND_TOOL = "web_search_20250305"        # Haiku 4.5 takes the basic search tool only
FIND_MAX_USES = 3
FIND_CAP_USD = 0.25
FIND_PURPOSE = "research_candidates"
_FIGURE_RES = {
    "range": re.compile(r"(?<![\d,.$])(\d{1,3})\s*(?:-\s*)?(?:miles?|mi\b)", re.I),
    "tow": re.compile(r"(?<![\d.$])(\d{1,2},\d{3}|\d{4,5})\s*(?:-\s*)?(?:lbs?\b|pounds\b)", re.I),
}


def _find_ask(kind: str, key: dict[str, Any]) -> str:
    what = describe(kind, key)
    if kind == "range":
        return (f"Find the published electric driving range (miles, electric only) for this vehicle: {what}. "
                "Prefer the manufacturer's site or fueleconomy.gov. In your reply, state the figure, the model year and "
                "trim it applies to, and the URL of the page that states it.")
    return (f"Find the manufacturer's maximum trailer tow rating in pounds for this vehicle: {what}. Prefer the "
            "manufacturer's site or its official towing guide. In your reply, state the rating, the model year, engine "
            "and tow equipment it applies to, and the URL of the page that states it.")


def figures_in(kind: str, text: str) -> list[int]:
    lo, hi = LIMITS[kind]
    out = []
    for m in _FIGURE_RES[kind].finditer(text or ""):
        n = int(m.group(1).replace(",", ""))
        if lo <= n <= hi and n not in out:
            out.append(n)
    return out


def find_candidates(kind: str, key: dict[str, Any], *, client: Any = None, run: Any = None) -> dict[str, Any]:
    """One Haiku 4.5 search (basic tool, a loose reply) for this configuration, capped at FIND_CAP_USD and logged
    under purpose research_candidates. Returns {"candidates": [{figure, source_url, quote, origin}], "cost", "reply",
    "error"}. Figures come from the citations (the quoted page text) and, marked as such, from the reply itself.
    They are stored as candidates only: entering a figure stays manual."""
    import api_cost
    import bounded_search

    run = run or bounded_search.run_search
    client = client or api_cost.make_client(max_retries=0)
    before = _logged_cost()
    out: dict[str, Any] = {"candidates": [], "cost": 0.0, "reply": "", "error": None}
    try:
        resp = run(client, kwargs={"model": FIND_MODEL, "max_tokens": 1500,
                                   "tools": [{"type": FIND_TOOL, "name": "web_search", "max_uses": FIND_MAX_USES}]},
                   messages=[{"role": "user", "content": _find_ask(kind, key)}], label=f"find candidates {describe(kind, key)}",
                   purpose=FIND_PURPOSE, max_cost_usd=FIND_CAP_USD)
    except bounded_search.SearchUnavailable as exc:
        out["error"] = f"{exc.reason}: {exc}"
        out["cost"] = round(_logged_cost() - before, 4)
        return out
    out["cost"] = round(_logged_cost() - before, 4)
    seen: set[tuple[int, str]] = set()
    text = ""
    for b in getattr(resp, "content", None) or []:
        if getattr(b, "type", None) != "text":
            continue
        text += getattr(b, "text", "") or ""
        for c in getattr(b, "citations", None) or []:
            quote = " ".join((getattr(c, "cited_text", "") or "").split())
            url = getattr(c, "url", "") or ""
            for n in figures_in(kind, quote):
                if (n, url) not in seen:
                    seen.add((n, url))
                    out["candidates"].append({"figure": n, "source_url": url, "quote": quote[:400], "origin": "Find candidates (quoted page)"})
    out["reply"] = text.strip()[:1500]
    urls = re.findall(r"https?://[^\s<>\")\]]+", text)
    for n in figures_in(kind, text):
        if not any(c["figure"] == n for c in out["candidates"]):
            out["candidates"].append({"figure": n, "source_url": urls[0].rstrip(".,;") if urls else "",
                                      "quote": "", "origin": "Find candidates (model's reply, no quote)"})
    if out["candidates"]:
        add_candidates(kind, key, [{"figure": c["figure"], "url": c["source_url"], "quote": c["quote"], "origin": c["origin"]}
                                   for c in out["candidates"]], "Find candidates")
    return out


def _logged_cost() -> float:
    import api_cost

    try:
        with contextlib.closing(sqlite3.connect(api_cost.db_path())) as c:
            c.executescript(api_cost.SCHEMA)
            return float(c.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM api_cost_log").fetchone()[0])
    except sqlite3.Error:
        return 0.0
