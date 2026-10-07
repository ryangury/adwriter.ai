#!/usr/bin/env python3
"""towing.py — verified tow ratings for the exact configuration on the sticker.

A tow figure reaches an ad only when it was found on a manufacturer page or a
manufacturer tow guide for exactly this configuration:

    year, make, model, engine, drivetrain   (+ cab and bed for trucks)

Everything in the key comes from what is printed (inventory record, trim, the
window sticker's engine line, cab and bed). The lookup is one web search limited
to the make's own domains and its fleet tow-guide domains; Python then rejects a
reply whose page was not among the results, is on another domain, or whose
printed year / make / model / engine / drivetrain / cab / bed differ from the
key. Results — including "no exact match" — are cached in feature_cache.db's
towing_config table under the full key. No match means no figure: the ad states
none and the car is listed under TOWING RATING NEEDS REVIEW.

A battery-electric car has no engine line: its key is the powertrain class plus
the trim (and drivetrain, cab / bed), and a page for another body style of the
same model (EQE Sedan vs EQE SUV) never matches.

tow_overrides.json ({VIN: {"rating", "source", "date"}}, editable on each car's
Database page) beats any lookup. A verified rating or an override becomes
TOWING_SENTENCE ("It is rated to tow up to 7,700 lbs."), a required sentence.

    from towing import towing_for
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from feature_cache import DB_PATH
from powertrain import MANUFACTURER_DOMAINS, MANUFACTURER_SEARCH_MODEL, sticker_engine_phrase, split_ymm

# --- trigger ------------------------------------------------------------------ #
# Any of these words in the sticker text or an option name means the car may
# tow: a hitch, a tow / trailering package, trailer wiring or a brake controller.
TOW_TRIGGER_RE = re.compile(r"\bhitch(?:es)?\b|\btow(?:ing)?\b|\btrailer(?:ing|s)?\b", re.IGNORECASE)

# --- where a rating may come from -------------------------------------------- #
# The make's own domains (powertrain.MANUFACTURER_DOMAINS) plus its own fleet /
# tow-guide sites. Dealer blogs, forums, Edmunds, Cars.com and news sites are
# never a source.
TOW_GUIDE_DOMAINS: dict[str, list[str]] = {
    "chevrolet": ["gmfleet.com", "gm.com"],
    "gmc": ["gmfleet.com", "gm.com"],
    "cadillac": ["gmfleet.com", "gm.com"],
    "buick": ["buick.com", "gmfleet.com", "gm.com"],
    "ford": ["fordpro.com"],
    "lincoln": ["fordpro.com"],
    "ram": ["ramtrucks.com", "stellantisnorthamerica.com"],
    # Stellantis's own media site publishes each model year's official
    # specifications, towing tables included (jeep.com refuses automated fetches).
    "jeep": ["stellantisnorthamerica.com"],
    "dodge": ["stellantisnorthamerica.com"],
    "chrysler": ["stellantisnorthamerica.com"],
}
TOW_LOOKUP_MAX_USES = 6
NO_MATCH_RECHECK_DAYS = 30  # a cached "no exact match" is looked up again after this

TRUCK_MODELS_RE = re.compile(
    r"\b(?:Silverado|Sierra|Ram\s+\d{4}|F-?\d{3}|Super\s+Duty|Tundra|Tacoma|Ranger|"
    r"Colorado|Canyon|Gladiator|Frontier|Titan|Ridgeline|Maverick|Santa\s+Cruz)\b",
    re.IGNORECASE,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS towing_config (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    year            INTEGER NOT NULL,
    make            TEXT NOT NULL COLLATE NOCASE,
    model           TEXT NOT NULL COLLATE NOCASE,
    engine          TEXT NOT NULL COLLATE NOCASE,
    drivetrain      TEXT NOT NULL COLLATE NOCASE,
    cab             TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    bed             TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    tow_package     TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    tow_rating_lbs  INTEGER,           -- NULL: no exact match on an allowed page
    page_config     TEXT,              -- the configuration as the page printed it
    source_url      TEXT,
    note            TEXT,
    checked_date    TEXT NOT NULL,
    UNIQUE (year, make, model, engine, drivetrain, cab, bed, tow_package)
);
"""


@contextlib.contextmanager
def _connect():
    """A feature_cache.db connection that commits on success and is always
    closed (sqlite3's own `with conn:` commits but leaves the file open)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(towing_config)")]
        if cols and "tow_package" not in cols:
            # First version keyed without the tow package: those rows can't
            # say which package their rating needs, so they are dropped and
            # looked up again.
            conn.execute("DROP TABLE towing_config")
        conn.executescript(_SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


# --- the configuration ------------------------------------------------------- #

_DRIVE_TOKENS = {"AWD": "AWD", "4MATIC": "AWD", "4MOTION": "AWD", "QUATTRO": "AWD", "XDRIVE": "AWD",
                 "4WD": "4WD", "4X4": "4WD", "FWD": "FWD", "RWD": "RWD", "2WD": "2WD", "4X2": "2WD"}
_CAB_WORDS = r"(CREW|DOUBLE|REGULAR|EXTENDED|QUAD|MEGA|SUPER\s?CREW|SUPER\s?CAB)"
# On the sticker the word CAB must follow ("REGULAR UNLEADED" is not a cab);
# a page's cab field is already just the cab.
_CAB_STICKER_RE = re.compile(rf"\b{_CAB_WORDS}\s*CAB\b", re.I)
_CAB_RE = re.compile(rf"\b{_CAB_WORDS}\b", re.I)
_BED_WORD_RE = re.compile(r"\b(SHORT|STANDARD|LONG)\s*(?:BED|BOX)\b", re.I)
_BED_FEET_RE = re.compile(r"(\d)\s*(?:'|FT\.?|-FT\.?|FOOT)\s*(\d{1,2})?\s*(?:\"|IN\.?)?\s*(?:BED|BOX|CARGO BOX)", re.I)


# Factory tow equipment as printed: the rating often depends on it (GM's Max
# Trailering Package, Ford's Class II/IV packages, Mercedes' hitch option), so
# it is part of the key and a page's required package must be among these.
_TOW_EQUIP_RE = re.compile(
    r"MAX(?:IMUM)?\s+TRAILERING(?:\s+PACKAGE)?|HEAVY[- ]DUTY\s+TRAILER(?:ING)?\s+(?:TOW\s+)?PACKAGE|"
    r"CLASS\s+(?:I{1,3}V?|IV|[1-4])\s+TRAILER\s+TOW\s+PACKAGE|TRAILER(?:ING)?\s+(?:TOW\s+)?PACKAGE|TOW(?:ING)?\s+PACKAGE|"
    r"TOWING\s+PREP(?:\s+PACKAGE)?|TRAILER\s+HITCH|TOW\s+HITCH|(?:CLASS\s+(?:III|IV|3|4)\s+)?RECEIVER\s+HITCH|INCREASED\s+TOWING",
    re.IGNORECASE,
)


def tow_equipment(sticker_text: str | None) -> str:
    """The sticker's printed tow equipment, normalized and sorted ("" if none)."""
    found = {" ".join(m.group(0).upper().split()) for m in _TOW_EQUIP_RE.finditer(sticker_text or "")}
    return "; ".join(sorted(found))


def normalize_drive(text: str | None) -> str | None:
    for tok in re.findall(r"[A-Za-z0-9]+", (text or "").upper()):
        if tok in _DRIVE_TOKENS:
            return _DRIVE_TOKENS[tok]
    return None


def _bed_class(feet: float) -> str:
    return "short" if feet < 6.0 else "standard" if feet < 7.5 else "long"


def normalize_bed(text: str | None) -> str | None:
    t = text or ""
    m = _BED_WORD_RE.search(t)
    if m:
        return m.group(1).lower()
    m = _BED_FEET_RE.search(t)
    if m:
        return _bed_class(int(m.group(1)) + int(m.group(2) or 0) / 12)
    return None


def normalize_cab(text: str | None, *, sticker: bool = False) -> str | None:
    m = (_CAB_STICKER_RE if sticker else _CAB_RE).search(text or "")
    return re.sub(r"\s+", "", m.group(1)).lower() if m else None


# Trucks sold in exactly one cab and one bed, so the model name implies both
# when the sticker prints neither. Each entry is checked against the maker's own
# specifications: (make, model regex) -> (cab, bed, source).
IMPLIED_CAB_BED: list[tuple[str, re.Pattern, str, str, str]] = [
    ("jeep", re.compile(r"^gladiator\b", re.I), "crew", "short",
     "Stellantis 2021 Jeep Gladiator specifications: one body (fuel tank '(4-door)'), "
     "one box (60.3 in, tailgate closed)"),
]


def implied_cab_bed(make: str | None, model: str | None) -> tuple[str, str, str] | None:
    """(cab, bed, source) for a single-cab, single-bed model, else None."""
    for mk, rx, cab, bed, src in IMPLIED_CAB_BED:
        if (make or "").lower() == mk and rx.search(model or ""):
            return cab, bed, src
    return None


def is_truck(model: str | None, body_style: str | None = None) -> bool:
    return (body_style or "").lower() == "truck" or bool(TRUCK_MODELS_RE.search(model or ""))


_DRIVE_WORDS_RE = re.compile(r"\b(?:AWD|4MATIC\+?|4MOTION|QUATTRO|XDRIVE|4WD|4X4|FWD|RWD|2WD|4X2)\b", re.I)


def ev_trim(trim: str | None) -> str | None:
    """The trim without its drivetrain word ("Long Range Plus AWD" -> "Long
    Range Plus", "EQE 500 AWD" -> "EQE 500")."""
    t = " ".join(_DRIVE_WORDS_RE.sub(" ", trim or "").split())
    return t or None


def vehicle_config(
    year_make_model: str | None,
    trim: str | None,
    body_style: str | None,
    sticker_text: str | None,
    powertrain_class: str | None = None,
) -> dict[str, Any]:
    """The configuration a tow rating must match, from printed data only.
    "missing" lists any part that could not be read (no rating can match then).
    A battery-electric car has no engine: its key is the powertrain class plus
    the trim (stored in the engine column as "battery-electric <trim>")."""
    year, make, model = split_ymm(year_make_model)
    ev = powertrain_class == "bev"
    if ev:
        t = ev_trim(trim)
        engine = f"battery-electric {t}" if t else None
    else:
        engine = sticker_engine_phrase(sticker_text)
    if not ev and not engine and make and make.lower().startswith("mercedes"):
        # Mercedes-Benz stickers print no engine line; the model designation in
        # the trim ("GLE 450") names the engine variant.
        m = re.search(r"\b([A-Z]{1,4}[- ]?\d{2,3}[a-z]?)\b", trim or "")
        engine = m.group(1).replace("-", " ") if m else None
    drive = normalize_drive(trim) or normalize_drive(sticker_text)
    truck = is_truck(model, body_style)
    cab = normalize_cab(sticker_text, sticker=True) if truck else ""
    bed = normalize_bed(sticker_text) if truck else ""
    implied = None
    if truck and (not cab or not bed):
        implied = implied_cab_bed(make, model)
        if implied:
            cab, bed = cab or implied[0], bed or implied[1]
    cfg = {
        "year": year, "make": make, "model": model, "engine": engine, "drivetrain": drive,
        "cab": cab, "bed": bed, "truck": truck, "tow_package": tow_equipment(sticker_text),
        "ev": ev, "body": (body_style or "").strip(),
        "implied_cab_bed": implied[2] if implied else None,
    }
    cfg["missing"] = [k for k in ("year", "make", "model", "engine", "drivetrain") if not cfg[k]] + (
        [k for k in ("cab", "bed") if truck and not cfg[k]]
    )
    return cfg


def describe(cfg: dict[str, Any]) -> str:
    parts = [str(cfg.get("year") or "?"), cfg.get("make") or "?", cfg.get("model") or "?",
             cfg.get("engine") or "engine ?", cfg.get("drivetrain") or "drivetrain ?"]
    if cfg.get("truck"):
        parts += [f"{cfg.get('cab') or '?'} cab", f"{cfg.get('bed') or '?'} bed"]
    parts.append(f"[tow equipment: {cfg.get('tow_package') or 'none printed'}]")
    return " ".join(parts)


def package_problem(required: str, sticker_tow: str) -> str | None:
    """A rating that needs a package is only for a car whose sticker prints it:
    the required package's distinguishing words must be in the printed tow
    equipment. None when nothing is required or it is printed."""
    req = (required or "").strip()
    if not req or req.lower() in ("none", "n/a", "standard", "no package", "not required"):
        return None
    # Words that set the package apart ("MAX", "CLASS", "IV", "HEAVY", "HITCH");
    # generic ones and option codes ("550") are left out.
    generic = {"PACKAGE", "PKG", "WITH", "THE", "AND", "FACTORY", "OPTIONAL", "OPTION", "EQUIPPED",
               "TRAILERING", "TRAILER", "TOW", "TOWING", "CODE", "INCLUDES", "REQUIRED"}
    words = [w for w in re.findall(r"\b[A-Z]{3,}\b|\bI{1,3}V?\b|\bIV\b", req.upper()) if w not in generic]
    have = (sticker_tow or "").upper()
    if not have:
        return f"the rating requires {req!r}; the sticker prints no tow equipment"
    missing = [w for w in words if not re.search(rf"\b{re.escape(w)}\b", have)]
    if missing:
        return f"the rating requires {req!r}, which the sticker does not print (it prints {sticker_tow})"
    return None


def triggered(sticker_text: str | None, option_names: list[str] | None) -> bool:
    return bool(TOW_TRIGGER_RE.search(sticker_text or "")) or any(
        TOW_TRIGGER_RE.search(n or "") for n in option_names or []
    )


# --- matching a page against the configuration ---------------------------------- #

_DISP_RE = re.compile(r"(?<![\d.])(\d\.\d)\s*-?\s*L(?:ITER)?\b", re.I)


def engine_matches(cfg_engine: str, page_engine: str, page_model: str = "") -> bool:
    """The page's engine is the sticker's: the same displacement, and diesel /
    hybrid on both or neither. A Mercedes-Benz designation ("GLE 450", used
    because the sticker prints no engine) matches when the page names the same
    designation number in its engine or model field."""
    ce, pe = cfg_engine.lower(), (page_engine or "").lower()
    disp = _DISP_RE.findall(cfg_engine)
    if not disp:
        num = re.search(r"\d{2,3}", cfg_engine)
        return bool(num) and num.group(0) in f"{pe} {(page_model or '').lower()}"
    if not pe or not set(disp) & set(_DISP_RE.findall(page_engine)):
        return False
    for word, alts in (("diesel", ("diesel", "duramax", "power stroke", "cummins", "ecodiesel")),
                       ("hybrid", ("hybrid",))):
        on_sticker, on_page = word in ce, any(a in pe for a in alts)
        if on_sticker != on_page:
            return False
    return True


def match_problems(cfg: dict[str, Any], page: dict[str, str]) -> list[str]:
    """Why the page's printed configuration is not this vehicle's ([] = exact)."""
    probs = []
    if str(cfg["year"]) not in (page.get("year") or ""):
        probs.append(f"year {page.get('year')!r} is not {cfg['year']}")
    if (cfg["make"] or "").lower().split("-")[0] not in (page.get("make") or "").lower():
        probs.append(f"make {page.get('make')!r} is not {cfg['make']}")
    model_words = [w for w in re.findall(r"[A-Za-z0-9-]+", cfg["model"] or "") if w.lower() not in ("class",)]
    if not all(w.lower() in (page.get("model") or "").lower() for w in model_words):
        probs.append(f"model {page.get('model')!r} is not {cfg['model']}")
    if cfg.get("ev"):
        # Battery-electric: the page names this trim (in its model or engine /
        # trim field) and no combustion or hybrid powertrain.
        trim = cfg["engine"].removeprefix("battery-electric ")
        where = f"{page.get('model') or ''} {page.get('engine') or ''}".lower()
        if not all(w.lower() in where for w in re.findall(r"[A-Za-z0-9+-]+", trim)):
            probs.append(f"trim: the page ({page.get('model')!r} / {page.get('engine')!r}) is not {trim!r}")
        if re.search(r"\bhybrid\b|\bgas(?:oline)?\b|\bdiesel\b|\d\.\dL\b", page.get("engine") or "", re.I):
            probs.append(f"powertrain {page.get('engine')!r} is not battery-electric")
    elif not engine_matches(cfg["engine"], page.get("engine") or "", page.get("model") or ""):
        probs.append(f"engine {page.get('engine')!r} is not the sticker's {cfg['engine']!r}")
    # A page for another body style of the same model (EQE Sedan vs EQE SUV,
    # which tow differently) is not this vehicle.
    page_bodies = set(re.findall(r"\b(sedan|suv|coupe|wagon|convertible|cabriolet|hatchback)\b", (page.get("model") or "").lower()))
    ours = (cfg.get("body") or "").lower()
    if page_bodies and ours in ("sedan", "suv", "coupe", "wagon", "convertible", "cabriolet", "hatchback") and ours not in page_bodies:
        probs.append(f"body {sorted(page_bodies)} is not {ours}")
    page_drive = page.get("drivetrain") or ""
    drives = {_DRIVE_TOKENS[t] for t in re.findall(r"[A-Za-z0-9]+", page_drive.upper()) if t in _DRIVE_TOKENS}
    if cfg["drivetrain"] not in drives:
        probs.append(f"drivetrain {page_drive!r} is not {cfg['drivetrain']}")
    if cfg.get("truck"):
        if normalize_cab(page.get("cab")) != cfg["cab"]:
            probs.append(f"cab {page.get('cab')!r} is not {cfg['cab']}")
        if normalize_bed(f"{page.get('bed') or ''} bed") != cfg["bed"]:
            probs.append(f"bed {page.get('bed')!r} is not {cfg['bed']}")
    return probs


# --- the lookup ------------------------------------------------------------------ #

_TOW_LINE_RE = re.compile(
    r"^TOW\s*::(?P<lbs>[^:]*)::(?P<year>[^:]*)::(?P<make>[^:]*)::(?P<model>[^:]*)::(?P<engine>[^:]*)"
    r"::(?P<drivetrain>[^:]*)::(?P<cab>[^:]*)::(?P<bed>[^:]*)::(?P<package>[^:]*)::\s*(?P<url>\S+)\s*$",
    re.M,
)


def allowed_domains(make: str | None) -> list[str]:
    key = (make or "").lower().split("-")[0].split()[0] if make else ""
    if (make or "").lower().startswith("mercedes"):
        key = "mercedes-benz"
    return MANUFACTURER_DOMAINS.get(key, []) + TOW_GUIDE_DOMAINS.get(key, [])


def parse_reply(text: str, cfg: dict[str, Any], domains: list[str], seen_urls: set[str]) -> dict[str, Any]:
    """Validate the one-line lookup reply: {"lbs", "url", "page_config", "note"}."""
    m = _TOW_LINE_RE.search(text or "")
    if not m:
        return {"lbs": None, "note": "tow lookup returned no TOW line"}
    page = {k: m.group(k).strip() for k in ("year", "make", "model", "engine", "drivetrain", "cab", "bed", "package")}
    lbs_raw, url = m.group("lbs").strip().replace(",", ""), m.group("url").strip()
    page_config = " | ".join(f"{k}: {v}" for k, v in page.items() if v and v.lower() != "n/a")
    if not lbs_raw.isdigit():
        return {"lbs": None, "url": url, "page_config": page_config,
                "note": "no allowed page publishes a rating for this exact configuration"}
    host = urllib.parse.urlparse(url).netloc.lower()
    if not any(host == d or host.endswith("." + d) for d in domains):
        return {"lbs": None, "url": url, "page_config": page_config,
                "note": f"rejected: {host} is not a manufacturer or tow-guide domain ({', '.join(domains)})"}
    if url.rstrip("/") not in {u.rstrip("/") for u in seen_urls}:
        return {"lbs": None, "url": url, "page_config": page_config,
                "note": "rejected: the cited page was not among the pages searched"}
    probs = match_problems(cfg, page)
    pkg_prob = package_problem(page.get("package") or "", cfg.get("tow_package") or "")
    if pkg_prob:
        probs.append(pkg_prob)
    if probs:
        return {"lbs": None, "url": url, "page_config": page_config, "note": "rejected: " + "; ".join(probs)}
    return {"lbs": int(lbs_raw), "url": url, "page_config": page_config, "note": None}


class TowLookupUnavailable(RuntimeError):
    """The search call itself failed (network / rate limit); nothing is cached."""


def lookup(cfg: dict[str, Any]) -> dict[str, Any]:
    """One web search limited to allowed_domains(make) for this configuration."""
    import anthropic
    from credentials import ANTHROPIC_API_KEY

    domains = allowed_domains(cfg["make"])
    if not domains:
        return {"lbs": None, "note": f"no manufacturer domain on file for {cfg['make']}"}
    truck = (
        f", {cfg['cab']} cab, {cfg['bed']} bed" if cfg.get("truck") else ""
    )
    equip = cfg.get("tow_package") or "none printed"
    if cfg.get("ev"):
        what = (f"{cfg['year']} {cfg['make']} {cfg['model']} {cfg.get('body') or ''}, battery-electric, "
                f"trim {cfg['engine'].removeprefix('battery-electric ')}, {cfg['drivetrain']}{truck}")
        unit = "this trim"
        engine_field = "<trim as the page prints it, and 'electric'>"
    else:
        what = f"{cfg['year']} {cfg['make']} {cfg['model']}, engine {cfg['engine']}, {cfg['drivetrain']}{truck}"
        unit = "this engine"
        engine_field = "<engine>"
    ask = (
        f"Find the manufacturer-published maximum trailer tow rating for this exact vehicle: {what}. "
        f"Its window sticker prints this tow equipment: {equip}. "
        "Use web search; only the manufacturer's own pages or its official towing / trailering guide "
        f"count. The rating must be printed for this model year AND {unit} AND this drivetrain"
        + (" AND this cab and bed length" if cfg.get("truck") else "")
        + " AND the tow equipment this vehicle has: if the page gives several ratings by package "
        "(for example with and without a max trailering package or a higher-capacity hitch), give the "
        "one for the package this sticker prints, and name the package that rating requires. "
        "Never give a figure from memory, from a different year, engine, drivetrain"
        + (", cab or bed" if cfg.get("truck") else "")
        + " or package, or from a dealer, forum, review or news site.\n\n"
        "Reply with exactly one line and nothing else, copying each field as the page prints it:\n"
        f"TOW :: <pounds, digits only, or NONE> :: <model year> :: <make> :: <model and body style> :: {engine_field} :: "
        "<drivetrain> :: <cab or n/a> :: <bed length or n/a> :: <package the rating requires, or none> :: <page URL>"
    )
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    messages: list[Any] = [{"role": "user", "content": ask}]
    try:
        response = None
        for _ in range(4):
            response = client.messages.create(
                model=MANUFACTURER_SEARCH_MODEL,
                max_tokens=16000,
                tools=[{
                    "type": "web_search_20260209", "name": "web_search",
                    "allowed_domains": domains, "max_uses": TOW_LOOKUP_MAX_USES,
                }],
                messages=messages,
            )
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})
    except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.APIStatusError) as exc:
        raise TowLookupUnavailable(f"tow lookup failed: {exc}") from exc
    if response is None or response.stop_reason == "refusal":
        return {"lbs": None, "note": "tow lookup declined"}
    seen: set[str] = set()
    for block in response.content:
        if getattr(block, "type", None) == "web_search_tool_result" and isinstance(block.content, list):
            seen |= {getattr(r, "url", "") for r in block.content}
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    return parse_reply(text, cfg, domains, seen)


# --- cache ----------------------------------------------------------------------- #

def _key(cfg: dict[str, Any]) -> tuple:
    return (cfg["year"], cfg["make"], cfg["model"], cfg["engine"], cfg["drivetrain"],
            cfg["cab"] or "", cfg["bed"] or "", cfg.get("tow_package") or "")


def get_cached(cfg: dict[str, Any]) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM towing_config WHERE year=? AND make=? AND model=? AND engine=? "
            "AND drivetrain=? AND cab=? AND bed=? AND tow_package=?",
            _key(cfg),
        ).fetchone()
    if not row:
        return None
    row = dict(row)
    if row["tow_rating_lbs"] is None:
        checked = date.fromisoformat(row["checked_date"])
        if date.today() - checked > timedelta(days=NO_MATCH_RECHECK_DAYS):
            return None  # an old "no match" is looked up again
    return row


def save(cfg: dict[str, Any], result: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO towing_config (year, make, model, engine, drivetrain, cab, bed, tow_package, tow_rating_lbs, "
            "page_config, source_url, note, checked_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (year, make, model, engine, drivetrain, cab, bed, tow_package) DO UPDATE SET "
            "tow_rating_lbs=excluded.tow_rating_lbs, page_config=excluded.page_config, "
            "source_url=excluded.source_url, note=excluded.note, checked_date=excluded.checked_date",
            _key(cfg) + (result.get("lbs"), result.get("page_config"), result.get("url"),
                         result.get("note"), date.today().isoformat()),
        )


# --- the one entry point ---------------------------------------------------------- #

def towing_for(
    year_make_model: str | None,
    trim: str | None,
    body_style: str | None,
    sticker_text: str | None,
    option_names: list[str] | None = None,
    *,
    vin: str | None = None,
    powertrain_class: str | None = None,
    allow_lookup: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """{"triggered", "rating" (lbs or None), "config", "config_text", "source_url",
    "note", "override", "sentence"}. A tow override for the VIN beats everything
    (its rating is used verbatim). Not triggered -> nothing to state. Triggered
    without a rating -> the ad states no tow figure and "note" says why
    (TOWING RATING NEEDS REVIEW)."""
    cfg = vehicle_config(year_make_model, trim, body_style, sticker_text, powertrain_class)
    # force: check even without tow wording on the sticker (an ad that already
    # states a tow figure, e.g. a Tesla with no cached sticker).
    out = {"triggered": force or triggered(sticker_text, option_names), "rating": None, "config": cfg,
           "config_text": describe(cfg), "source_url": None, "note": None, "override": None,
           "sentence": None}
    ov = load_tow_overrides().get((vin or "").strip().upper()) if vin else None
    if ov:
        out.update(triggered=True, rating=ov["rating"], source_url=f"override: {ov.get('source') or ''}".strip(),
                   override=ov, sentence=towing_sentence(ov["rating"]))
        return out
    if not out["triggered"]:
        return out
    if cfg["missing"]:
        out["note"] = f"configuration incomplete on the sticker / record (no {', '.join(cfg['missing'])})"
        return out
    row = get_cached(cfg)
    if row is None and allow_lookup:
        try:
            result = lookup(cfg)
        except TowLookupUnavailable as exc:
            out["note"] = str(exc)
            return out
        save(cfg, result)
        row = {"tow_rating_lbs": result.get("lbs"), "source_url": result.get("url"), "note": result.get("note")}
        print(f"[towing] {out['config_text']}: {result.get('lbs') or 'no exact match'} "
              f"({result.get('url') or '-'}{'; ' + result['note'] if result.get('note') else ''})", file=sys.stderr)
    if row is None:
        out["note"] = "not looked up yet"
        return out
    out["rating"] = row.get("tow_rating_lbs")
    out["source_url"] = row.get("source_url")
    out["note"] = None if out["rating"] else (row.get("note") or "no exact match on a manufacturer page")
    out["sentence"] = towing_sentence(out["rating"])
    return out


# --- the sentence ------------------------------------------------------------------ #

def towing_sentence(rating: int | None) -> str | None:
    """TOWING_SENTENCE, built from a verified rating or an override, verbatim."""
    return f"It is rated to tow up to {rating:,} lbs." if rating else None


# --- overrides (tow_overrides.json, keyed by VIN) ----------------------------------- #

TOW_OVERRIDES_PATH = Path(__file__).with_name("tow_overrides.json")


def load_tow_overrides(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """{VIN: {"rating", "source", "date"}}; entries without a positive rating are ignored."""
    path = path or TOW_OVERRIDES_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"[towing] could not read {path.name}: {exc}", file=sys.stderr)
        return {}
    out = {}
    for vin, v in (data or {}).items():
        if isinstance(v, dict) and isinstance(v.get("rating"), int) and v["rating"] > 0:
            out[str(vin).strip().upper()] = v
    return out


def set_tow_override(
    vin: str, rating: int | None, source: str | None = None, path: Path | None = None
) -> dict[str, Any] | None:
    """Set (or, with rating None / 0, remove) one VIN's tow override. Returns the
    stored entry, or None when removed. A rating must be 500-40,000 lbs."""
    path = path or TOW_OVERRIDES_PATH
    vin = vin.strip().upper()
    if rating is not None and rating != 0 and not (500 <= int(rating) <= 40000):
        raise ValueError(f"tow rating {rating!r} lbs is out of range (500-40,000)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    entry = None
    if rating:
        entry = {"rating": int(rating), "source": (source or "").strip(), "date": date.today().isoformat()}
        data[vin] = entry
    else:
        data.pop(vin, None)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return entry


# --- old cache --------------------------------------------------------------------- #

def purge_unkeyed_towing(dry_run: bool = False) -> list[dict[str, Any]]:
    """Delete every towing_capacity row (keyed by year/make/model/trim, never by
    engine) and return them for the record."""
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM towing_capacity ORDER BY year, make, model")]
        if not dry_run:
            conn.execute("DELETE FROM towing_capacity")
    return rows


# --- ad text check ---------------------------------------------------------------- #

_TOW_FIGURE_RE = re.compile(
    r"\b(\d{1,2},\d{3})\s*(?:-\s*)?(?:lbs?\.?|pounds)\b[^.]*?\btow|\btow[^.]*?\b(\d{1,2},\d{3})\s*(?:lbs?\.?|pounds)\b",
    re.IGNORECASE,
)


def tow_figures(sentence: str) -> list[int]:
    return [int((a or b).replace(",", "")) for a, b in _TOW_FIGURE_RE.findall(sentence or "")]


def unverified_tow_sentences(text: str, rating: int | None) -> list[str]:
    """Sentences stating a tow figure other than the verified `rating` (every
    tow figure, when there is none)."""
    out = []
    for s in re.split(r"(?<=[.!?])\s+", text or ""):
        figs = tow_figures(s)
        if figs and any(f != rating for f in figs):
            out.append(s.strip())
    return out
