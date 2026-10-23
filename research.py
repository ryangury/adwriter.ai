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

BUSY_TIMEOUT_S = 30
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
    # read at call time (the tests point it at a temp file); wait out a writer instead of failing at once
    conn = sqlite3.connect(str(feature_cache.DB_PATH), timeout=BUSY_TIMEOUT_S)
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

    if pt.get("class") in PLUG_IN and not rng.get("miles") and not rng.get("waived"):
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
    return {k: c.get(k) for k in ("figure", "source_url", "quote", "origin", "found_date")}


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
        exact = tow.get("sentence") and any(" ".join(u.split()) == tow["sentence"] for u in TR._tow_units(entry["current_ad_text"]))
        same = figs and all(f == rating for f in figs)
        # keep only when the ad already states the figure in TOWING_SENTENCE's words (an override's qualifier included)
        action = "keep" if same and (exact or len(figs) == 1 and not (tow.get("override") or {}).get("qualifier")) \
            else ("replace" if figs else "add")
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
                waived = get_waiver("range", cfg)
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
                if waived and not rng.get("miles"):
                    rng["waived"] = waived
                pt["range"] = rng
            sticker = (get_window_sticker(vin) or {}) if vin else {}
            names = [o.get("name") or "" for o in (sticker.get("option_packages") or []) + (sticker.get("added_options_all") or [])]
            tow = towing_for(ymm, trim, body, sticker.get("raw_text") or "", names, vin=vin, powertrain_class=cls,
                             allow_lookup=False, vin_decode="fetch")   # the page / nightly job: complete a missing engine
            items = items_for(pt, tow)
            for it in items:
                it["config"] = (pt.get("range") or {}).get("config") if it["kind"] == "range" else tow.get("config")
        except Exception as exc:  # noqa: BLE001 - one car must not break the page
            items = [{"kind": "?", "missing": "range / tow", "reason": "other", "detail": f"could not be checked ({exc})",
                      "config_text": ymm or "?", "key_id": None, "candidates": []}]
        if items:
            out.append({"stock": stock, "ymm": ymm, "items": items})
    return out


# --------------------------------------------------------------------------- #
# "No figure needed": a configuration a person has decided states no figure
# --------------------------------------------------------------------------- #
# Stored by configuration (the key id; an incomplete configuration by its text). A waived configuration leaves the
# Research page and no sentence is built for it: towing_for() treats a waived tow configuration as not triggered (no
# TOWING_SENTENCE, no review flag), and vehicle_powertrain() gives a waived range configuration no range and no flag.
# A VIN override still wins. Undo removes the row (the configuration comes back on the next page load).
_WAIVER_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_waiver (
    kind          TEXT NOT NULL,
    waiver_id     TEXT NOT NULL,
    config_text   TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL,
    entered_date  TEXT NOT NULL,
    UNIQUE (kind, waiver_id)
);
"""


@contextlib.contextmanager
def _connect_waiver():
    with _connect() as conn:
        conn.executescript(_WAIVER_SCHEMA)
        yield conn


def waiver_id(kind: str, cfg: dict[str, Any] | None, config_text: str | None = None) -> str:
    """The configuration's key id, or for an incomplete configuration "incomplete:<configuration text>"."""
    key = key_of(kind, cfg) if cfg and not cfg.get("missing") else None
    if key:
        return key_id(kind, key)
    return "incomplete:" + " ".join((config_text or (cfg or {}).get("config_text") or "").split())


def set_waiver(kind: str, wid: str, reason: str | None, config_text: str | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    why = " ".join((reason or "").split())
    if not why:
        raise ValueError("give the reason no figure is needed")
    if not wid or wid == "incomplete:":
        raise ValueError("unknown configuration")
    row = {"kind": kind, "waiver_id": wid, "config_text": config_text or "", "reason": why[:500],
           "entered_date": date.today().isoformat()}
    with _connect_waiver() as conn:
        conn.execute("INSERT INTO config_waiver (kind, waiver_id, config_text, reason, entered_date) VALUES (?,?,?,?,?) "
                     "ON CONFLICT (kind, waiver_id) DO UPDATE SET reason=excluded.reason, config_text=excluded.config_text, "
                     "entered_date=excluded.entered_date", tuple(row.values()))
    return row


def clear_waiver(kind: str, wid: str) -> bool:
    with _connect_waiver() as conn:
        return conn.execute("DELETE FROM config_waiver WHERE kind=? AND waiver_id=?", (kind, wid)).rowcount > 0


def vin_waiver_id(vin: str) -> str:
    """A "No figure needed" for one car (a row whose configuration is incomplete can't be keyed by configuration)."""
    return "vin:" + (vin or "").strip().upper()


def get_waiver(kind: str, cfg: dict[str, Any] | None, config_text: str | None = None,
               vin: str | None = None) -> dict[str, Any] | None:
    """The waiver for this configuration, else for this VIN, or None. Never raises (a build must not sink on it)."""
    try:
        wids = [waiver_id(kind, cfg, config_text)] + ([vin_waiver_id(vin)] if vin else [])
        with _connect_waiver() as conn:
            for wid in wids:
                row = conn.execute("SELECT * FROM config_waiver WHERE kind=? AND waiver_id=?", (kind, wid)).fetchone()
                if row:
                    return dict(row)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[research] waiver check failed: {exc}", file=sys.stderr)
        return None


def all_waivers() -> list[dict[str, Any]]:
    with _connect_waiver() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM config_waiver ORDER BY kind, config_text")]


# --------------------------------------------------------------------------- #
# The Research page (computed at page load; read-only)
# --------------------------------------------------------------------------- #
SECTIONS = ("tow_stated", "tow_missing", "range")
SECTION_TITLES = {
    "tow_stated": "Live ads stating a tow figure with no allowed source",
    "tow_missing": "Hitch or tow package on the sticker, no figure",
    "range": "Electric range",
}
PLAIN = {
    "never": "Never looked up",
    "candidates": "Candidates found but not confirmed",
    "incomplete": "Configuration incomplete",
    "no_epa": "No EPA listing for this year",
    "conflict": "Conflicting sources",
    "no_match": "Looked up; no figure for this exact configuration",
}


_PART_WORDS = {"engine": "its engine", "bed": "its bed length", "cab": "its cab style", "drivetrain": "its drivetrain",
               "year": "its model year", "make": "its make", "model": "its model"}


def incomplete_text(cfg: dict[str, Any] | None) -> str:
    """The plain-English line on a row whose configuration is incomplete."""
    parts = [_PART_WORDS.get(m, m) for m in ((cfg or {}).get("missing") or ["engine"])]
    what = " or ".join(parts)
    return (f"This car's sticker doesn't print {what}, so a figure can't be shared with other cars. "
            "Enter a figure for this car.")


def plain_reason(kind: str, key: dict[str, Any] | None, reason_code: str | None, cands: list[dict[str, Any]]) -> str:
    """One of PLAIN's keys. Candidates of OTHER configurations (the tow cache's) are not this configuration's."""
    if not key:
        return "incomplete"
    # pass 0's own verdict first: its candidates for a "no entry" are other model years / body styles, not conflicts
    if reason_code == "epa_ambiguous":
        return "conflict"
    if reason_code == "epa_no_entry":
        return "no_epa"
    own = sorted({c["figure"] for c in cands if c.get("figure") and "OTHER configuration" not in (c.get("origin") or "")})
    if len(own) > 1:
        return "conflict"
    if own:
        return "candidates"
    if reason_code == "config_incomplete":
        return "incomplete"
    if reason_code == "no_exact_match":
        return "no_match"
    return "never"


def _live(stock: str, v: dict[str, Any], history: dict[str, Any]) -> bool:
    """In the snapshot, not absent or sold per ad_history."""
    e = history.get(stock) or {}
    return bool(v) and not e.get("absent_since") and (e.get("lifecycle_stage") or "") not in ("sold", "absent", "deleted")


def page_rows(vehicles: list[dict[str, Any]], history: dict[str, Any]) -> dict[str, Any]:
    """{"sections": {name: [row]}, "counts": {...}, "waived": [row]}. One row per configuration with the cars sharing
    it. Read-only: the snapshot, ad_history (read), the caches and this module's tables; no lookup, no write.

    tow_stated   a live ad states a tow figure that is not from a VIN override or a Research-page entry (or differs
                 from it): stock, stated figure(s), where the figure came from. Every one of these ads loses the
                 sentence at its next reprice (adwriter.strip_unverified_tow), waived or not.
    tow_missing  the sticker prints a hitch / tow package and there is no allowed figure (cars already in tow_stated
                 are not repeated).
    range        a plug-in hybrid / battery-electric car with no range."""
    import tow_refresh as TR
    import towing as T

    snap = {v.get("stock_number"): v for v in vehicles}
    old_rows = TR._old_cache_rows()
    rows: dict[str, dict[str, dict[str, Any]]] = {s: {} for s in SECTIONS}
    waived: dict[tuple[str, str], dict[str, Any]] = {}
    losing: list[str] = []
    stated_stocks: set[str] = set()

    def add(section: str, kind: str, cfg: dict[str, Any] | None, config_text: str, reason_code: str | None,
            detail: str, car: dict[str, Any]) -> None:
        key = key_of(kind, cfg) if cfg and not cfg.get("missing") else None
        wid = waiver_id(kind, cfg, config_text)
        cands = rank_candidates(kind, key, [_cand(c) for c in candidates(kind, key)]) if key else []
        car = dict(car, vin=(snap.get(car["stock"]) or {}).get("vin"))
        car["vin_waiver_id"] = vin_waiver_id(car["vin"]) if car["vin"] else None
        w = get_waiver(kind, cfg, config_text, vin=car["vin"])
        target = waived.setdefault((kind, wid), {"kind": kind, "waiver_id": wid, "config_text": config_text, "waiver": w,
                                                  "section": section, "cars": []}) if w else \
            rows[section].setdefault(wid, {
                "kind": kind, "section": section, "waiver_id": wid, "key_id": key_id(kind, key) if key else None,
                "config_text": describe(kind, key) if key else config_text, "needs_vin_override": key is None,
                "missing_parts": (cfg or {}).get("missing") or [], "incomplete_text": incomplete_text(cfg) if key is None else None,
                "reason": PLAIN[plain_reason(kind, key, reason_code, cands)],
                "reason_code": reason_code, "detail": detail, "candidates": cands, "fact": get_fact(kind, key) if key else None,
                "google_q": google_query(kind, describe(kind, key) if key else config_text),
                "engine_note": (f"Engine from VIN decode: {cfg.get('vin_engine_text')}"
                                if (cfg or {}).get("engine_source") == "VIN decode" else None),
                "vin_conflict": (cfg or {}).get("vin_decode_conflict"),
                "unit": UNITS[kind], "cars": []})
        target["cars"].append(car)

    for stock, e in sorted(history.items()):
        v = snap.get(stock)
        if not v or not _live(stock, v, history) or not e.get("current_ad_text"):
            continue
        units = TR._tow_units(e["current_ad_text"])
        figs = sorted({f for u in units for f in T.tow_figures(u)})
        if not figs:
            continue
        tow = TR._towing(stock, v, allow_lookup=False, vin_decode="fetch")   # the page / nightly job
        allowed = tow.get("rating") if T.rating_source(tow) else None
        if allowed and all(f == allowed for f in figs):
            continue
        losing.append(stock)
        stated_stocks.add(stock)
        code = tow_reason(tow.get("note")) if not allowed else "other"
        src = TR._source_of(figs, v.get("year_make_model"), e.get("last_feedback"), old_rows)
        add("tow_stated", "tow", tow.get("config"), tow.get("config_text") or "", code, tow.get("note") or "",
            {"stock": stock, "ymm": v.get("year_make_model"), "stated": figs, "source": src, "sentences": units,
             "allowed": allowed})

    for r in live_items([v for s, v in snap.items() if _live(s, v, history)]):
        for it in r["items"]:
            if it["kind"] == "tow" and r["stock"] in stated_stocks:
                continue
            section = "range" if it["kind"] == "range" else "tow_missing"
            cfg = it.get("config")
            add(section, it["kind"], cfg, it.get("config_text") or "", it.get("reason"), it.get("detail") or "",
                {"stock": r["stock"], "ymm": r.get("ymm")})

    sections = {s: sorted(rows[s].values(), key=lambda g: (not g["needs_vin_override"], g["config_text"])) for s in SECTIONS}
    cars = {c["stock"] for s in SECTIONS for g in sections[s] for c in g["cars"]}
    counts = {
        "rows": {s: len(sections[s]) for s in SECTIONS},
        "cars": {s: len({c["stock"] for g in sections[s] for c in g["cars"]}) for s in SECTIONS},
        "cars_total": len(cars),
        "losing_tow_sentence": len(losing),
        "needs_vin_override": sum(1 for s in SECTIONS for g in sections[s] if g["needs_vin_override"]),
        "waived": len(waived),
    }
    return {"sections": sections, "counts": counts, "waived": all_waivers(), "losing": losing}


# --------------------------------------------------------------------------- #
# "Run free EPA check": pass 0 for every range row; no model call
# --------------------------------------------------------------------------- #
def run_epa_check(vehicles: list[dict[str, Any]], history: dict[str, Any], *, lookup: Any = None) -> dict[str, Any]:
    """Pass 0 (fueleconomy.gov's data service, HTTP only, no model call) for every range row on the page, ignoring the
    30-day negative cache. An exact match is cached in the range cache with source "epa" and its fueleconomy.gov URL
    (the next build states it); a miss refreshes the negative entry and stores pass 0's candidates. Returns
    {"checked", "cleared", "remain", "results": [{config_text, stocks, status, miles, note}]}."""
    from feature_cache import save_electric_range
    from powertrain import RangeLookupUnavailable, cached_texts, classify, epa_lookup, split_ymm

    lookup = lookup or epa_lookup
    before = page_rows(vehicles, history)["sections"]["range"]
    snap = {v.get("stock_number"): v for v in vehicles}
    results, cleared = [], 0
    for g in before:
        v = snap.get(g["cars"][0]["stock"]) or {}
        year, make, model = split_ymm(v.get("year_make_model"))
        trim = v.get("trim")
        res: dict[str, Any] = {"config_text": g["config_text"], "stocks": [c["stock"] for c in g["cars"]]}
        if not (year and make and model):
            results.append(dict(res, status="skipped", note="no year / make / model"))
            continue
        st, rc = cached_texts(v.get("vin")) if v.get("vin") else ("", "")
        cls = classify(v.get("vin"), v.get("year_make_model"), trim, sticker_text=st, recon_text=rc)["class"]
        try:
            r = lookup(year, make, model, trim, cls, body_style=v.get("body_style"), extra_text=" ".join(st[:400].split()))
        except RangeLookupUnavailable as exc:
            results.append(dict(res, status="unavailable", note=str(exc)))
            continue
        if r["status"] == "match":
            save_electric_range(year, make, model, trim, powertrain=cls, range_miles=r["miles"], source="epa",
                                matched_trim=r.get("matched_trim"), source_url=r.get("url"), note=None)
            cleared += 1
            results.append(dict(res, status="match", miles=r["miles"], url=r.get("url"), note=r.get("matched_trim")))
            continue
        neg = "ambiguous" if r["status"] in ("ambiguous", "conflict") else "none"
        save_electric_range(year, make, model, trim, powertrain=cls, range_miles=None, source=neg, matched_trim=None,
                            source_url=None, note=r.get("note"))
        if r.get("candidates") and g.get("key_id"):
            kind, key = key_from_id(g["key_id"])
            add_candidates("range", key, r["candidates"], "fueleconomy.gov (pass 0)")
        results.append(dict(res, status=r["status"], note=r.get("note")))
    remain = len(page_rows(vehicles, history)["sections"]["range"])
    return {"checked": len(before), "cleared": cleared, "remain": remain, "results": results}


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


def find_candidates(kind: str, key: dict[str, Any], *, client: Any = None, run: Any = None,
                    purpose: str = None, origin: str = "Find candidates") -> dict[str, Any]:
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
                   purpose=purpose or FIND_PURPOSE, max_cost_usd=FIND_CAP_USD)
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
                    out["candidates"].append({"figure": n, "source_url": url, "quote": quote[:400], "origin": f"{origin} (quoted page)"})
    out["reply"] = text.strip()[:1500]
    urls = re.findall(r"https?://[^\s<>\")\]]+", text)
    for n in figures_in(kind, text):
        if not any(c["figure"] == n for c in out["candidates"]):
            out["candidates"].append({"figure": n, "source_url": urls[0].rstrip(".,;") if urls else "",
                                      "quote": "", "origin": f"{origin} (model's reply, no quote)"})
    if out["candidates"]:
        add_candidates(kind, key, [{"figure": c["figure"], "url": c["source_url"], "quote": c["quote"], "origin": c["origin"]}
                                   for c in out["candidates"]], origin)
    return out


def _logged_cost() -> float:
    import api_cost

    try:
        with contextlib.closing(sqlite3.connect(api_cost.db_path())) as c:
            c.executescript(api_cost.SCHEMA)
            return float(c.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM api_cost_log").fetchone()[0])
    except sqlite3.Error:
        return 0.0


# --------------------------------------------------------------------------- #
# Candidate ranking for the Research page (Python only, no model call)
# --------------------------------------------------------------------------- #
EPA_DOMAINS = ("fueleconomy.gov", "epa.gov")
MEDIA_DOMAINS = ("caranddriver.com", "motortrend.com", "edmunds.com", "kbb.com", "cars.com", "autoblog.com",
                 "roadandtrack.com", "consumerreports.org", "autotrader.com", "thecarconnection.com", "motor1.com",
                 "cnet.com", "insideevs.com", "electrek.co", "greencarreports.com", "jdpower.com", "autoweek.com",
                 "carscoops.com", "topgear.com", "autocar.co.uk", "usnews.com", "truecar.com", "carfax.com")
_NON_US_PATH = re.compile(r"/(?!us/)(?:[a-z]{2}|en-(?!us/)[a-z]{2}|[a-z]{2}-[a-z]{2})/", re.I)
_WEAK_WORDS = re.compile(r"\breal[- ]world\b|\btested\b|\bour test\b", re.I)
_YEAR_RE = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
_GENERIC = {"engine", "awd", "rwd", "fwd", "4wd", "crew", "cab", "bed", "battery", "electric", "trim", "box", "short",
            "long", "standard"}


def _domain(url: str | None) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1).lower().removeprefix("www.") if m else "")


def _manufacturer_domains(make: str | None) -> tuple[str, ...]:
    out: list[str] = []
    try:
        import towing
        out += list(towing.allowed_domains(make) or [])
    except Exception:  # noqa: BLE001
        pass
    try:
        from powertrain import MANUFACTURER_DOMAINS
        out += list(MANUFACTURER_DOMAINS.get((make or "").lower()) or [])
    except Exception:  # noqa: BLE001
        pass
    return tuple(d.lower().removeprefix("www.") for d in out)


def _on(domain: str, domains: tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


def rank_candidates(kind: str, key: dict[str, Any] | None, cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The candidates, best first, each with display fields: "domain", "marks" (manufacturer / EPA / media),
    "weak" (non-US path, real-world / tested wording, another model year in the quote), "n_sources" (distinct
    domains stating the same figure) and "tier": a quoted page naming the right year and trim, then other quoted
    pages, then reply-only figures, then other configurations' cached figures. No model call."""
    key = key or {}
    mfr = _manufacturer_domains(key.get("make"))
    year = str(key.get("year") or "")
    tokens = {t for t in re.findall(r"[a-z0-9.]+", f"{key.get('model') or ''} {key.get('engine') or ''}".lower())
              if len(t) >= 2 and t not in _GENERIC}
    by_figure: dict[int, set[str]] = {}
    for c in cands:
        d = _domain(c.get("source_url"))
        if d and "OTHER configuration" not in (c.get("origin") or ""):
            by_figure.setdefault(c.get("figure"), set()).add(d)
    out = []
    for c in cands:
        c = dict(c)
        d = _domain(c.get("source_url"))
        quote = c.get("quote") or ""
        origin = c.get("origin") or ""
        c["domain"] = d
        c["marks"] = ([("manufacturer")] if d and _on(d, mfr) else []) + (["EPA"] if d and _on(d, EPA_DOMAINS) else []) \
            + (["media"] if d and _on(d, MEDIA_DOMAINS) else [])
        weak = []
        if _NON_US_PATH.search(re.sub(r"^https?://[^/]+", "", c.get("source_url") or "")):
            weak.append("non-US page")
        if _WEAK_WORDS.search(quote):
            weak.append("real-world / tested figure")
        other_years = sorted({y for y in _YEAR_RE.findall(quote) if year and y != year})
        if other_years:
            weak.append(f"quote names {', '.join(other_years)}")
        c["weak"] = weak
        c["n_sources"] = len(by_figure.get(c.get("figure"), set()))
        q = quote.lower()
        right = bool(quote) and year in quote and (not tokens or any(t in q for t in tokens))
        if "OTHER configuration" in origin:
            tier = 4
        elif not quote or "reply" in origin:
            tier = 3
        elif right:
            tier = 1
        else:
            tier = 2
        c["tier"] = tier
        c["tier_label"] = {1: "quoted page, right year and trim", 2: "quoted page", 3: "reply only, no quote",
                           4: "another configuration"}[tier]
        out.append(c)
    out.sort(key=lambda c: (c["tier"], -c["n_sources"], -len(c["marks"]), len(c["weak"]), -(int(str(c.get("found_date") or "0")[:4] or 0))))
    return out


def google_query(kind: str, config_text: str | None) -> str:
    """The "Check on Google" search text: the configuration plus what is wanted (no fetch, just a link)."""
    t = re.sub(r"\[[^\]]*\]", " ", config_text or "")
    t = " ".join(w for w in t.split() if w not in ("?", "engine", "drivetrain") or w.isdigit())
    return f"{t} {'EPA electric range' if kind == 'range' else 'towing capacity'}".strip()


# --------------------------------------------------------------------------- #
# Auto Find candidates (python research.py --auto-candidates [--dry-run])
# --------------------------------------------------------------------------- #
# Daily (AdWriter-ResearchCandidates, 9:30 AM): one Find candidates search per configuration on the Research page
# that has no entry, isn't "No figure needed", has a live car, isn't config_incomplete and had no auto search in the
# last AUTO_RECHECK_DAYS days. Priority: live ads stating a tow figure with no allowed source, then hitch / tow
# package with no figure, then range rows with no EPA entry. Candidates only: never ad_history, never an entry.
AUTO_PURPOSE = "research_candidates_auto"
AUTO_ORIGIN = "Auto search"
AUTO_MAX_SEARCHES = 12
AUTO_MAX_COST_USD = 2.00
AUTO_RECHECK_DAYS = 30
AUTO_LOCK_WAIT_S = 90 * 60
AUTO_EST_COST_USD = 0.075     # mean logged cost of a Find candidates search so far (basic tool, 3 uses)
_AUTO_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_search (
    kind          TEXT NOT NULL,
    key_id        TEXT NOT NULL,
    config_text   TEXT NOT NULL DEFAULT '',
    searched_date TEXT NOT NULL,
    result        TEXT NOT NULL,          -- found | none | error
    n_candidates  INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    error         TEXT,
    UNIQUE (kind, key_id)
);
CREATE TABLE IF NOT EXISTS config_search_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date      TEXT NOT NULL,
    started       TEXT NOT NULL,
    finished      TEXT NOT NULL,
    planned       INTEGER NOT NULL,
    searched      INTEGER NOT NULL,
    found         INTEGER NOT NULL,
    cost_usd      REAL NOT NULL,
    stopped       TEXT
);
"""


@contextlib.contextmanager
def _connect_auto():
    with _connect() as conn:
        conn.executescript(_AUTO_SCHEMA)
        yield conn


def last_auto_search(kind: str, kid: str) -> dict[str, Any] | None:
    with _connect_auto() as conn:
        row = conn.execute("SELECT * FROM config_search WHERE kind=? AND key_id=?", (kind, kid)).fetchone()
    return dict(row) if row else None


def _record_search(kind: str, kid: str, config_text: str, result: str, n: int, cost: float, error: str | None,
                   today: str) -> None:
    with _connect_auto() as conn:
        conn.execute("INSERT INTO config_search (kind, key_id, config_text, searched_date, result, n_candidates, cost_usd, error) "
                     "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (kind, key_id) DO UPDATE SET config_text=excluded.config_text, "
                     "searched_date=excluded.searched_date, result=excluded.result, n_candidates=excluded.n_candidates, "
                     "cost_usd=excluded.cost_usd, error=excluded.error",
                     (kind, kid, config_text, today, result, n, round(cost, 4), error))


def last_auto_run() -> dict[str, Any] | None:
    try:
        with _connect_auto() as conn:
            row = conn.execute("SELECT * FROM config_search_run ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def auto_summary_line() -> str:
    """The Build Summary line, from the auto run's last result."""
    r = last_auto_run()
    if not r:
        return "Research candidates found last night: no auto run yet"
    return (f"Research candidates found last night: {r['found']} configuration{'' if r['found'] == 1 else 's'}, "
            f"${r['cost_usd']:.2f} ({r['run_date']}; {r['searched']} searched"
            + (f"; stopped: {r['stopped']}" if r.get("stopped") else "") + ")")


AUTO_SECTIONS = ("tow_stated", "tow_missing", "range")


def auto_plan(vehicles: list[dict[str, Any]], history: dict[str, Any], *, today: date | None = None
              ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(rows to search in priority order, rows skipped with the reason). Read-only."""
    today = today or date.today()
    data = page_rows(vehicles, history)
    snap = {v.get("stock_number"): v for v in vehicles}
    todo, skipped = [], []
    for section in AUTO_SECTIONS:
        for g in data["sections"][section]:
            row = {"section": section, "kind": g["kind"], "key_id": g.get("key_id"), "config_text": g["config_text"],
                   "stocks": [c["stock"] for c in g["cars"]], "reason": g.get("reason")}
            live = [s for s in row["stocks"] if snap.get(s) and _live(s, snap[s], history)]
            last = last_auto_search(g["kind"], g["key_id"]) if g.get("key_id") else None
            why = None
            if not g.get("key_id"):
                why = "configuration incomplete"
            elif g.get("fact"):
                why = "entry exists"
            elif not live:
                why = "no live car"
            elif section == "range" and g.get("reason_code") != "epa_no_entry":
                why = "range row without a 'no EPA entry' result (run the free EPA check first)"
            elif last:
                try:
                    age = (today - date.fromisoformat(last["searched_date"])).days
                except ValueError:
                    age = AUTO_RECHECK_DAYS + 1
                if age < AUTO_RECHECK_DAYS:
                    why = f"auto search {age} day(s) ago ({last['result']})"
            (skipped if why else todo).append(dict(row, skip=why) if why else row)
    return todo, skipped


def _lock_held(lock_path: Path | None = None) -> bool:
    from run_lock import ORCHESTRATOR_LOCK_PATH, lock_blocks_edit

    return lock_blocks_edit(lock_path or ORCHESTRATOR_LOCK_PATH) is not None


def run_auto_candidates(vehicles: list[dict[str, Any]], history: dict[str, Any], *, dry_run: bool = False,
                        find: Any = None, lock_held: Any = None, sleep: Any = None, clock: Any = None,
                        spent: Any = None, today: date | None = None, log: Any = print) -> dict[str, Any]:
    """Run (or with dry_run, only list) the auto searches. Limits per run: AUTO_MAX_SEARCHES searches and
    AUTO_MAX_COST_USD of actual logged cost (a search is not started when spent + FIND_CAP_USD would pass it). Before
    each search's cache write it waits for orchestrator.lock to clear (up to AUTO_LOCK_WAIT_S from the start). It
    takes no lock itself and never writes ad_history. Returns the run's summary."""
    import time

    find = find or find_candidates
    lock_held = lock_held or _lock_held
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    spent = spent or _logged_cost
    today = today or date.today()
    todo, skipped = auto_plan(vehicles, history, today=today)
    summary: dict[str, Any] = {"planned": len(todo), "skipped": skipped, "todo": todo, "searched": 0, "found": 0,
                               "cost": 0.0, "stopped": None, "results": [],
                               "estimate": round(len(todo[:AUTO_MAX_SEARCHES]) * AUTO_EST_COST_USD, 2),
                               "worst_case": round(min(len(todo[:AUTO_MAX_SEARCHES]) * FIND_CAP_USD, AUTO_MAX_COST_USD), 2)}
    if dry_run:
        return summary
    started_wall = datetime.now().isoformat(timespec="seconds")
    start, base = clock(), spent()
    for row in todo:
        cost_so_far = round(spent() - base, 4)
        if summary["searched"] >= AUTO_MAX_SEARCHES:
            summary["stopped"] = f"{AUTO_MAX_SEARCHES}-search limit reached"
            break
        if cost_so_far + FIND_CAP_USD > AUTO_MAX_COST_USD:
            summary["stopped"] = f"${AUTO_MAX_COST_USD:.2f} cap reached (${cost_so_far:.2f} logged)"
            break
        waited = False
        while lock_held():
            if clock() - start >= AUTO_LOCK_WAIT_S:
                summary["stopped"] = "orchestrator.lock still held after 90 minutes"
                break
            if not waited:
                log("[auto-candidates] orchestrator.lock is held - waiting before writing the cache")
                waited = True
            sleep(30)
        if summary["stopped"]:
            break
        kind, key = key_from_id(row["key_id"])
        before = spent()
        try:
            res = find(kind, key, purpose=AUTO_PURPOSE, origin=AUTO_ORIGIN)
            err = res.get("error")
        except Exception as exc:  # noqa: BLE001 - one search must not stop the run
            res, err = {"candidates": []}, f"{type(exc).__name__}: {exc}"
        cost = round(spent() - before, 4)
        result = "error" if err else ("found" if res.get("candidates") else "none")
        _record_search(kind, row["key_id"], row["config_text"], result, len(res.get("candidates") or []), cost, err,
                       today.isoformat())
        summary["searched"] += 1
        summary["found"] += result == "found"
        summary["results"].append(dict(row, result=result, n=len(res.get("candidates") or []), cost=cost, error=err))
        log(f"[auto-candidates] {row['config_text']}: {result} ({len(res.get('candidates') or [])} candidate(s), ${cost:.3f})"
            + (f" - {err}" if err else ""))
    summary["cost"] = round(spent() - base, 4)
    if not summary["stopped"] and len(todo) > summary["searched"]:
        summary["stopped"] = "stopped early"
    with _connect_auto() as conn:
        conn.execute("INSERT INTO config_search_run (run_date, started, finished, planned, searched, found, cost_usd, stopped) "
                     "VALUES (?,?,?,?,?,?,?,?)", (today.isoformat(), started_wall, datetime.now().isoformat(timespec="seconds"),
                                                  len(todo), summary["searched"], summary["found"], summary["cost"],
                                                  summary["stopped"]))
    return summary


def _main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Research tools")
    ap.add_argument("--auto-candidates", action="store_true", help="search for candidates on the Research page's rows")
    ap.add_argument("--dry-run", action="store_true", help="list the rows it would search and the estimated cost")
    args = ap.parse_args(argv)
    if not args.auto_candidates:
        ap.print_help()
        return 2
    here = Path(__file__).with_name("last_inventory_snapshot.json")
    vehicles = json.loads(here.read_text(encoding="utf-8"))["vehicles"]
    history = json.loads(here.with_name("ad_history.json").read_text(encoding="utf-8"))      # read only
    if not args.dry_run:
        import api_cost

        api_cost.use_production_key(purpose=AUTO_PURPOSE)       # a production job (prints the key class, never a key)
    s = run_auto_candidates(vehicles, history, dry_run=args.dry_run)
    print(f"\n[auto-candidates] {'DRY RUN - ' if args.dry_run else ''}{s['planned']} row(s) to search "
          f"(limit {AUTO_MAX_SEARCHES} per run, ${AUTO_MAX_COST_USD:.2f}); {len(s['skipped'])} skipped")
    for r in s["todo"]:
        print(f"  {'SEARCH' if args.dry_run else 'row'}  [{r['section']}] {r['config_text']}  ({', '.join(r['stocks'])}) - {r['reason']}")
    for r in s["skipped"]:
        print(f"  skip    [{r['section']}] {r['config_text']}  ({', '.join(r['stocks'])}) - {r['skip']}")
    if args.dry_run:
        n = min(s["planned"], AUTO_MAX_SEARCHES)
        print(f"  estimated cost: ${s['estimate']:.2f} for {n} search(es) at ~${AUTO_EST_COST_USD:.3f} each "
              f"(worst case ${s['worst_case']:.2f} at the ${FIND_CAP_USD:.2f} cap)")
    else:
        print(f"  searched {s['searched']}, found {s['found']}, ${s['cost']:.2f}" + (f"; stopped: {s['stopped']}" if s["stopped"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
