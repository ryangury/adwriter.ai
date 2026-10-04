#!/usr/bin/env python3
"""powertrain.py — powertrain class and the ELECTRIC_RANGE data field.

Class (one of CLASSES, or "unknown"):

    phev         plug-in hybrid
    bev          battery-electric
    hybrid       standard hybrid
    mild_hybrid  mild hybrid (48-volt / EQ Boost / e-supercharger)
    gas          no electrification signal

classify() collects signals in priority order — the window sticker's EPA /
fuel-type text, the sticker's model code and option lines, ReconVision's
"EV (Electric Vehicle) Inspection", then make / model / trim patterns. Each
signal names the classes it allows; the class is what every signal allows.
When the signals disagree (or only narrow it to several plug-in / hybrid
classes) and there is no override, the class is "unknown": no range is
stated and a flag goes into the feedback. A model name ending in "e" is never
a signal on its own (GLC 350e is a plug-in hybrid, G 580e is battery-electric).

Overrides: powertrain_overrides.json, keyed by VIN, edited from the Database
page (/cache/<stock>). An override always wins.

ELECTRIC_RANGE (plug-in hybrid / battery-electric only): fueleconomy.gov's
public data service (ws/rest/vehicle) returns the EPA range directly, so it is
looked up there for the exact year, make, model and trim — never taken from
the window sticker or from model memory. No exact-year EPA match falls back to
the manufacturer's own figure for that year and trim (a web search limited to
the manufacturer's site); a trim that matches only a different model year is
never used. Two conflicting EPA results, or no figure at all, omit the range
and flag it. Every result is cached per year / make / model / trim in
feature_cache.db (electric_range), so each trim is looked up once.

check_claims() / strip_violations() are the post-generation check used by
generate, reprice and the recon top-up: a stated range must be within
RANGE_TOLERANCE_MILES of ELECTRIC_RANGE (the prompts ask for its exact
phrase; the check compares the number), powertrain-type words must fit the
class, and a car with no ELECTRIC_RANGE (standard hybrids included) states
no range.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Callable

PHEV = "phev"
BEV = "bev"
HYBRID = "hybrid"
MILD = "mild_hybrid"
GAS = "gas"
UNKNOWN = "unknown"

CLASSES = (PHEV, BEV, HYBRID, MILD, GAS)
PLUG_IN = frozenset({PHEV, BEV})
CLASS_LABELS = {
    PHEV: "plug-in hybrid",
    BEV: "battery-electric",
    HYBRID: "standard hybrid",
    MILD: "mild hybrid",
    GAS: "gas",
    UNKNOWN: "unknown",
}

OVERRIDES_PATH = Path(__file__).with_name("powertrain_overrides.json")


# --------------------------------------------------------------------------- #
# overrides
# --------------------------------------------------------------------------- #


def load_overrides(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """{VIN: {"class", "note", "updated"}} from powertrain_overrides.json."""
    path = path or OVERRIDES_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"[powertrain] could not read {path.name}: {exc}", file=sys.stderr)
        return {}
    return {
        str(vin).strip().upper(): v
        for vin, v in (data or {}).items()
        if isinstance(v, dict) and v.get("class") in CLASSES
    }


def set_override(
    vin: str, cls: str | None, note: str | None = None, path: Path | None = None
) -> dict[str, Any] | None:
    """Set (or, with cls None / "", remove) the override for one VIN. Returns
    the stored entry, or None when removed."""
    path = path or OVERRIDES_PATH
    vin = vin.strip().upper()
    if cls and cls not in CLASSES:
        raise ValueError(f"unknown powertrain class {cls!r}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    entry = None
    if cls:
        entry = {"class": cls, "note": (note or "").strip(), "updated": date.today().isoformat()}
        data[vin] = entry
    else:
        data.pop(vin, None)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return entry


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

_PHEV_LABEL_RE = re.compile(
    r"Plug-?In\s*Hybrid\s*Vehicle|Electricity\s*[-+]\s*Gasoline|All\s*Electric\s*range", re.I
)
_BEV_LABEL_RE = re.compile(r"(?<!Hybrid )\bElectric\s+Vehicle\b", re.I)
_EPA_LABEL_RE = re.compile(
    r"fueleconomy\.gov|Fuel\s*Economy\s*and\s*Environment|The\s*best\s*vehicle\s*rates|Gasoline\s*Vehicle",
    re.I,
)
_HYBRID_TEXT_RE = re.compile(r"(?<!mild )(?<!mild-)\bHYB(?:RID)?\b", re.I)
_MILD_TEXT_RE = re.compile(r"\bMHEV\b|\bmild[- ]hybrid\b|\b48[- ]?V(?:olt)?\b|\bEQ\s*Boost\b", re.I)
# Python-built paragraph-two sentence for Mercedes-Benz mild hybrids whose
# sticker prints the 48-volt system line (see mild_sentence_for()).
MILD_HYBRID_SENTENCE = (
    "A 48-volt mild-hybrid system (Mercedes-Benz EQ Boost) adds a short electric boost, "
    "recovers braking energy and restarts the engine almost imperceptibly at stops. "
    "There is nothing to plug in."
)
_STICKER_48V_RE = re.compile(r"\b48[- ]?V(?:olt)?\b[^\n]{0,20}\bsystem\b", re.I)
_STICKER_MILD_TERM_RE = re.compile(r"\bmild[- ]hybrid\b|\bMHEV\b", re.I)
_MB_CODE_RE = re.compile(r"\b(?:19|20)\d\d\s+MERCEDES-BENZ\s+([A-Z0-9]+)\b")
_MB_BEV_CODE_RE = re.compile(r"^(?:EQ[A-Z]|G580)")
_MB_PHEV_CODE_RE = re.compile(r"^[A-Z]{1,4}\d{2,3}E[A-Z]?\d?$")
_CHARGING_HW_RE = re.compile(
    r"on-?board\s+(?:AC\s+)?charger|\bDC\s+(?:fast\s+)?charging|charging\s+socket|charge\s+port"
    r"|power\s+charger|(?<!USB )(?<!USB-C )charging\s+cable|\bwallbox\b",
    re.I,
)
_RV_EV_RE = re.compile(r"\bEV\s*\(Electric Vehicle\)\s*Inspection\b|\bEV\s+Inspection\b", re.I)

_NAME_PHEV_RE = re.compile(r"\bplug-?in\b|\bPHEV\b|\b4xe\b|\bEnergi\b", re.I)
_NAME_HYBRID_RE = re.compile(r"(?<!mild )(?<!mild-)\bhybrid\b", re.I)
_NAME_MILD_RE = re.compile(r"\bMHEV\b|\be-?SC\b|\bmild[- ]hybrid\b", re.I)
_NAME_BEV_RE = re.compile(
    r"\bEQ[ABCES]\b|\bEQS\b|\bEQE\b|\bwith EQ Technology\b|\be-?tron\b|\bIoniq\s*[56]\b|\bEV[369]\b"
    r"|\bMach-?E\b|\bLightning\b|\bBolt\b|\bLeaf\b|\bLyriq\b|\bTaycan\b|\biX\b|\bi[4579]\b"
    r"|\bID\.?\s?4\b|\bbZ4X\b|Pure\s+Electric|\bC40\b|\bEX30\b|\bEX90\b",
    re.I,
)
_BEV_MAKES = {"tesla", "rivian", "polestar", "lucid"}


def _signal(group: str, name: str, classes: set[str], evidence: str) -> dict[str, Any]:
    return {"group": group, "signal": name, "classes": sorted(classes), "evidence": evidence}


def _snip(text: str, m: re.Match, width: int = 40) -> str:
    return " ".join(text[max(0, m.start() - width): m.end() + width].split())


def collect_signals(
    year_make_model: str | None,
    trim: str | None,
    sticker_text: str | None = None,
    recon_text: str | None = None,
) -> list[dict[str, Any]]:
    """Every powertrain signal found, in priority order."""
    st = sticker_text or ""
    sigs: list[dict[str, Any]] = []

    # 1. sticker EPA / fuel-type text
    m = _PHEV_LABEL_RE.search(st)
    if m:
        sigs.append(_signal("sticker EPA label", "plug-in hybrid label", {PHEV}, _snip(st, m)))
    else:
        m = _BEV_LABEL_RE.search(st)
        if m:
            sigs.append(_signal("sticker EPA label", "electric vehicle label", {BEV}, _snip(st, m)))
        else:
            m = _EPA_LABEL_RE.search(st)
            if m:
                sigs.append(_signal(
                    "sticker EPA label", "gasoline fuel-economy label",
                    {GAS, HYBRID, MILD}, _snip(st, m),
                ))
    m = _HYBRID_TEXT_RE.search(st)
    if m:
        sigs.append(_signal("sticker EPA label", "hybrid powertrain text", {HYBRID, PHEV}, _snip(st, m)))
    m = _MILD_TEXT_RE.search(st)
    if m:
        sigs.append(_signal("sticker EPA label", "mild-hybrid / 48-volt text", {MILD}, _snip(st, m)))

    # 2. sticker model code and option lines
    m = _MB_CODE_RE.search(st)
    if m:
        code = m.group(1)
        if _MB_BEV_CODE_RE.match(code):
            sigs.append(_signal("sticker model code", f"model code {code}", {BEV}, code))
        elif _MB_PHEV_CODE_RE.match(code):
            sigs.append(_signal("sticker model code", f"model code {code}", {PHEV}, code))
    m = _CHARGING_HW_RE.search(st)
    if m:
        sigs.append(_signal("sticker option lines", "plug-in charging hardware", {PHEV, BEV}, _snip(st, m)))

    # 3. ReconVision
    m = _RV_EV_RE.search(recon_text or "")
    if m:
        sigs.append(_signal("ReconVision", "EV Inspection", {PHEV, BEV}, m.group(0)))

    # 4. make / model / trim patterns (a trailing "e" alone is never a signal)
    name = f"{year_make_model or ''} {trim or ''}".strip()
    make = (name.split()[1] if len(name.split()) > 1 else "").lower()
    if make in _BEV_MAKES:
        sigs.append(_signal("make/model", f"{make.title()} builds only electric vehicles", {BEV}, name))
    for rx, classes, what in (
        (_NAME_PHEV_RE, {PHEV}, "plug-in name"),
        (_NAME_BEV_RE, {BEV}, "electric model name"),
        (_NAME_MILD_RE, {MILD}, "mild-hybrid name"),
    ):
        m = rx.search(name)
        if m:
            sigs.append(_signal("make/model", what, classes, m.group(0)))
    if not _NAME_PHEV_RE.search(name):
        m = _NAME_HYBRID_RE.search(name)
        if m:
            sigs.append(_signal("make/model", "hybrid name", {HYBRID}, m.group(0)))
    return sigs


def resolve(signals: list[dict[str, Any]]) -> tuple[str, str | None]:
    """(class, flag). The class every signal allows; "gas" with no signal at
    all or when only the broad gasoline-label signal applies; "unknown" (with
    a flag) when the signals disagree or leave several electrified classes."""
    if not signals:
        return GAS, None
    allowed = set(CLASSES)
    for s in signals:
        allowed &= set(s["classes"])
    if len(allowed) == 1:
        return allowed.pop(), None
    listed = "; ".join(f"{s['signal']} -> {'/'.join(s['classes'])}" for s in signals)
    if not allowed:
        return UNKNOWN, f"powertrain signals disagree ({listed}) — class unknown, no range stated; set an override on the Database page"
    if GAS in allowed:
        return GAS, None
    return UNKNOWN, f"powertrain signals do not settle the class ({listed}) — class unknown, no range stated; set an override on the Database page"


def classify(
    vin: str | None,
    year_make_model: str | None,
    trim: str | None,
    *,
    sticker_text: str | None = None,
    recon_text: str | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """{"class", "label", "source" ("override" | "signals" | "default"),
    "signals", "flag", "override"}."""
    signals = collect_signals(year_make_model, trim, sticker_text, recon_text)
    # "mild hybrid" is a selling term only when the sticker itself prints it
    # (a "48 Volt System" option line alone classes the car, but doesn't).
    sticker_mild = bool(_STICKER_MILD_TERM_RE.search(sticker_text or ""))
    m48 = _STICKER_48V_RE.search(sticker_text or "")
    sticker_48v = " ".join(m48.group(0).split()) if m48 else None
    ov = (overrides if overrides is not None else load_overrides()).get((vin or "").strip().upper())
    if ov:
        cls = ov["class"]
        return {
            "class": cls, "label": CLASS_LABELS[cls], "source": "override",
            "signals": signals, "flag": None, "override": ov, "sticker_mild": sticker_mild,
            "sticker_48v": sticker_48v,
        }
    cls, flag = resolve(signals)
    return {
        "class": cls, "label": CLASS_LABELS[cls],
        "source": "signals" if signals else "default",
        "signals": signals, "flag": flag, "override": None, "sticker_mild": sticker_mild,
        "sticker_48v": sticker_48v,
    }


def mild_sentence_for(info: dict[str, Any], make: str | None) -> str | None:
    """MILD_HYBRID_SENTENCE when every gate holds: Mercedes-Benz, class mild
    hybrid, and the window sticker prints the 48-volt system line."""
    if (make or "").strip().lower() != "mercedes-benz":
        return None
    if info.get("class") != MILD or not info.get("sticker_48v"):
        return None
    return MILD_HYBRID_SENTENCE


def mild_wording_ok(pt: dict[str, Any]) -> bool:
    """Whether the model's own mild-hybrid wording may stand: only when the
    sticker prints the term and no MILD_HYBRID_SENTENCE is carrying it (the
    term never appears twice)."""
    return bool(pt.get("sticker_mild")) and not pt.get("mild_sentence")


def cached_texts(vin: str | None) -> tuple[str, str]:
    """(sticker text, recon text) from vehicle_cache.db for a VIN: the
    sticker's raw text plus its option names, and the work order's page text."""
    if not vin:
        return "", ""
    from vehicle_cache import get_vehicle

    return texts_from_row(get_vehicle(vin.strip().upper()) or {})


def texts_from_row(row: dict[str, Any]) -> tuple[str, str]:
    """(sticker text, recon text) from one vehicle_cache.db row."""
    sticker_text = recon_text = ""
    try:
        st = json.loads(row.get("window_sticker_json") or "null") or {}
    except ValueError:
        st = {}
    if isinstance(st, dict):
        names = [
            o.get("name") or ""
            for key in ("option_packages", "standard_options", "added_options_all")
            for o in (st.get(key) or [])
            if isinstance(o, dict)
        ]
        sticker_text = "\n".join([st.get("raw_text") or "", *names])
    try:
        rc = json.loads(row.get("recon_json") or "null") or {}
    except ValueError:
        rc = {}
    if isinstance(rc, dict):
        recon_text = rc.get("raw_text") or ""
    return sticker_text, recon_text


def classify_vehicle(vin: str | None, year_make_model: str | None, trim: str | None) -> dict[str, Any]:
    """classify() with the sticker and recon text read from vehicle_cache.db."""
    sticker_text, recon_text = cached_texts(vin)
    return classify(vin, year_make_model, trim, sticker_text=sticker_text, recon_text=recon_text)


# --------------------------------------------------------------------------- #
# ELECTRIC_RANGE — fueleconomy.gov first, then the manufacturer
# --------------------------------------------------------------------------- #

EPA_BASE = "https://www.fueleconomy.gov/ws/rest/vehicle"
EPA_ATV = {BEV: "EV", PHEV: "Plug-in Hybrid"}
EPA_NEIGHBOR_YEARS = 2


class RangeLookupUnavailable(Exception):
    """A lookup service could not be reached; nothing is cached and the range
    is omitted for this run."""


def epa_fetch(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET one fueleconomy.gov data-service URL as JSON (None for an empty body)."""
    url = f"{EPA_BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "adwriter"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8").strip()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RangeLookupUnavailable(f"fueleconomy.gov unreachable: {exc}") from exc
    if not body or body == "null":
        return None
    try:
        return json.loads(body)
    except ValueError as exc:
        raise RangeLookupUnavailable(f"fueleconomy.gov returned non-JSON for {url}") from exc


def _menu(data: Any) -> list[dict[str, str]]:
    items = (data or {}).get("menuItem") if isinstance(data, dict) else None
    if isinstance(items, dict):
        items = [items]
    return [i for i in items or [] if isinstance(i, dict)]


_DESIG_RE = re.compile(r"\b([A-Za-z]{1,4})[\s-]?(\d{2,3})(?:[a-z])?(?!\d)")
_MODEL_LETTER_RE = re.compile(r"\bModel\s+([3SXY])\b", re.I)


def designations(text: str) -> set[str]:
    """Model designations in a name: "GLC350e 4matic" -> {"glc350"},
    "G 580 with EQ Technology" -> {"g580"}, "Model X Long Range" -> {"modelx"}."""
    out = {f"{a.lower()}{b}" for a, b in _DESIG_RE.findall(text or "")}
    out |= {f"model{m.lower()}" for m in _MODEL_LETTER_RE.findall(text or "")}
    return out


_BODY_TOKENS = {"suv", "coupe", "convertible", "wagon", "sedan", "cabriolet", "roadster", "van"}
_AWD_TOKENS = {"4matic", "awd", "4wd", "xdrive", "quattro", "4x4", "eawd"}
_TWO_WD_TOKENS = {"rwd", "fwd", "2wd"}
_FILLER_TOKENS = {"with", "eq", "technology", "tech", "hybrid", "plug", "in", "electric", "auto", "and"}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _desig_parts(desigs: set[str]) -> set[str]:
    parts = set(desigs)
    for d in desigs:
        m = re.match(r"([a-z]+)(\d+)$", d)
        if m:
            parts |= {m.group(1), m.group(2)}
        if d.startswith("model"):
            parts |= {"model", d[5:]}
    return parts


def _pick_model(
    names: list[str], ours: set[str], ours_desig: set[str], body: str | None, awd: bool | None
) -> tuple[list[str], str]:
    """Narrow candidate EPA model names to the one for this trim. Returns
    (survivors, why) — exactly one survivor is a match."""
    toks = {n: _tokens(n) for n in names}
    survivors = []
    for n in names:
        t = toks[n]
        others = [toks[o] for o in names if o != n]
        cbody = t & _BODY_TOKENS
        if cbody and body and body not in cbody:
            continue
        if not cbody and body and any(body in o for o in others):
            continue
        if t & _AWD_TOKENS and awd is False:
            continue
        if t & _TWO_WD_TOKENS and awd:
            continue
        if not t & _AWD_TOKENS and awd and any(o & _AWD_TOKENS for o in others):
            continue
        survivors.append(n)
    if len(survivors) <= 1:
        return survivors, "body/drivetrain"
    parts = _desig_parts(ours_desig)
    scored = []
    for n in survivors:
        words = toks[n] - parts - _BODY_TOKENS - _AWD_TOKENS - _TWO_WD_TOKENS - _FILLER_TOKENS
        words = {w for w in words if not w.isdigit()}
        if words - ours:
            continue  # the EPA name carries a trim word this car doesn't have
        scored.append((len(words & ours), n))
    if not scored:
        return survivors, "trim words"
    best = max(s for s, _ in scored)
    top = [n for s, n in scored if s == best]
    return top, "trim words"


def epa_lookup(
    year: int,
    make: str,
    model: str,
    trim: str | None,
    cls: str,
    *,
    body_style: str | None = None,
    extra_text: str = "",
    fetch: Callable[..., Any] = epa_fetch,
) -> dict[str, Any]:
    """Exact year/make/model/trim lookup on fueleconomy.gov. Returns
    {"status": "match", "miles", "matched_trim", "url"} |
    {"status": "conflict", "note"} | {"status": "none", "note"}.
    Raises RangeLookupUnavailable when the service can't be reached."""
    ours_text = f"{model} {trim or ''} {extra_text}"
    ours = _tokens(ours_text)
    ours_desig = designations(f"{model} {trim or ''}")
    body = (body_style or "").strip().lower() or None
    if body not in _BODY_TOKENS:
        body = None
    awd: bool | None = True if ours & _AWD_TOKENS else (False if ours & _TWO_WD_TOKENS else None)
    atv = EPA_ATV[cls]

    def year_matches(y: int) -> dict[str, list[tuple[str, str, int]]]:
        """{EPA model name: [(option text, vehicle id, miles), ...]} for this
        year's models that share a designation and have this powertrain."""
        names = [i.get("value") or "" for i in _menu(fetch("menu/model", {"year": y, "make": make}))]
        out: dict[str, list[tuple[str, str, int]]] = {}
        for name in names:
            if not (designations(name) & ours_desig):
                continue
            for opt in _menu(fetch("menu/options", {"year": y, "make": make, "model": name})):
                rec = fetch(str(opt.get("value"))) or {}
                if rec.get("atvType") != atv:
                    continue
                raw = rec.get("range") if cls == BEV else rec.get("rangeA")
                try:
                    miles = int(round(float(raw)))
                except (TypeError, ValueError):
                    continue
                if miles <= 0:
                    continue
                out.setdefault(name, []).append((opt.get("text") or "", str(rec.get("id") or opt.get("value")), miles))
        return out

    if not ours_desig:
        return {"status": "none", "note": f"no model designation found in {model!r} {trim!r} to match against fueleconomy.gov"}

    found = year_matches(year)
    if found:
        top, why = _pick_model(list(found), ours, ours_desig, body, awd)
        if len(top) > 1:
            listing = "; ".join(f"{n}: {sorted({m for _, _, m in found[n]})} mi" for n in top)
            return {"status": "conflict", "note": f"fueleconomy.gov {year}: more than one trim fits ({listing})"}
        if top:
            name = top[0]
            miles = sorted({m for _, _, m in found[name]})
            if len(miles) > 1:
                listing = "; ".join(f"{t} = {m} mi" for t, _, m in found[name])
                return {"status": "conflict", "note": f"fueleconomy.gov {year} {name}: conflicting ranges ({listing})"}
            vid = found[name][0][1]
            return {
                "status": "match", "miles": miles[0], "matched_trim": f"{year} {make} {name}",
                "url": f"https://www.fueleconomy.gov/feg/Find.do?action=sbs&id={vid}",
            }

    # No exact-year match: note any neighbouring year that does list this
    # trim (never used — the figure must be for this model year).
    elsewhere = []
    for y in range(year - EPA_NEIGHBOR_YEARS, year + EPA_NEIGHBOR_YEARS + 1):
        if y == year:
            continue
        other = year_matches(y)
        if other:
            top, _ = _pick_model(list(other), ours, ours_desig, body, awd)
            for n in top:
                elsewhere.append(f"{y} {n} = {sorted({m for _, _, m in other[n]})} mi")
    note = f"fueleconomy.gov has no {year} {make} {model} {trim or ''} {CLASS_LABELS[cls]} entry".replace("  ", " ")
    if elsewhere:
        note += f"; it lists only a different model year ({'; '.join(elsewhere)}), which is not used"
    return {"status": "none", "note": note}


MANUFACTURER_DOMAINS: dict[str, list[str]] = {
    "mercedes-benz": ["mbusa.com", "mercedes-benz.com"],
    "volvo": ["volvocars.com"],
    "tesla": ["tesla.com"],
    "polestar": ["polestar.com"],
    "rivian": ["rivian.com"],
    "lucid": ["lucidmotors.com"],
    "bmw": ["bmwusa.com"],
    "audi": ["audiusa.com"],
    "porsche": ["porsche.com"],
    "volkswagen": ["vw.com"],
    "ford": ["ford.com"],
    "lincoln": ["lincoln.com"],
    "chevrolet": ["chevrolet.com"],
    "cadillac": ["cadillac.com"],
    "gmc": ["gmc.com"],
    "jeep": ["jeep.com"],
    "chrysler": ["chrysler.com"],
    "dodge": ["dodge.com"],
    "toyota": ["toyota.com"],
    "lexus": ["lexus.com"],
    "honda": ["automobiles.honda.com"],
    "acura": ["acura.com"],
    "hyundai": ["hyundaiusa.com"],
    "genesis": ["genesis.com"],
    "kia": ["kia.com"],
    "nissan": ["nissanusa.com"],
    "mazda": ["mazdausa.com"],
    "subaru": ["subaru.com"],
    "mitsubishi": ["mitsubishicars.com"],
    "land": ["landroverusa.com"],
    "jaguar": ["jaguarusa.com"],
    "mini": ["miniusa.com"],
}

MANUFACTURER_SEARCH_MODEL = "claude-sonnet-4-6"  # same model as the ad-writing calls
_MFR_LINE_RE = re.compile(r"^RANGE\s*::\s*(.+?)\s*::\s*(.+?)\s*::\s*(.+?)\s*::\s*(\S+)\s*$", re.M)


def manufacturer_search(
    year: int, make: str, model: str, trim: str | None, cls: str, domains: list[str]
) -> dict[str, Any]:
    """The manufacturer's own published electric range for exactly this model
    year and trim, by a web search limited to the manufacturer's site.
    Returns {"miles": N or None, "matched_trim", "url", "note"}. The figure
    is accepted only when the stated page is one the search actually
    returned, on the manufacturer's domain, for this model year."""
    import anthropic
    from credentials import ANTHROPIC_API_KEY

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    ask = (
        f"Find the manufacturer-published electric driving range for the {year} {make} {model} "
        f"{trim or ''} ({CLASS_LABELS[cls]}). Use web search; only the manufacturer's own pages "
        f"count. The figure must be printed on a page for model year {year} and for this trim "
        "(or a page that states it applies to this trim). Never give a figure from memory, from "
        "a different model year or from a different trim.\n\n"
        "Reply with exactly one line and nothing else:\n"
        "RANGE :: <miles, digits only, or NONE> :: <model year printed on the page> :: "
        "<trim name exactly as the page prints it> :: <page URL>"
    )
    messages: list[Any] = [{"role": "user", "content": ask}]
    try:
        response = None
        for _ in range(4):
            response = client.messages.create(
                model=MANUFACTURER_SEARCH_MODEL,
                max_tokens=16000,
                tools=[{
                    "type": "web_search_20260209", "name": "web_search",
                    "allowed_domains": domains, "max_uses": 6,
                }],
                messages=messages,
            )
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})
    except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.APIStatusError) as exc:
        raise RangeLookupUnavailable(f"manufacturer search failed: {exc}") from exc
    if response is None or response.stop_reason == "refusal":
        return {"miles": None, "note": "manufacturer search declined"}
    seen_urls: set[str] = set()
    for block in response.content:
        if getattr(block, "type", None) == "web_search_tool_result" and isinstance(block.content, list):
            seen_urls |= {getattr(r, "url", "") for r in block.content}
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    return parse_manufacturer_reply(text, year, domains, seen_urls)


def parse_manufacturer_reply(
    text: str, year: int, domains: list[str], seen_urls: set[str]
) -> dict[str, Any]:
    """Validate the one-line manufacturer-search reply (see manufacturer_search)."""
    m = _MFR_LINE_RE.search(text or "")
    if not m:
        return {"miles": None, "note": "manufacturer search returned no RANGE line"}
    miles_raw, page_year, trim_name, url = (g.strip() for g in m.groups())
    if not miles_raw.isdigit():
        return {"miles": None, "note": f"manufacturer site has no {year} figure for this trim"}
    host = urllib.parse.urlparse(url).netloc.lower()
    if not any(host == d or host.endswith("." + d) for d in domains):
        return {"miles": None, "note": f"manufacturer figure rejected: {url} is not on {', '.join(domains)}"}
    if url.rstrip("/") not in {u.rstrip("/") for u in seen_urls}:
        return {"miles": None, "note": f"manufacturer figure rejected: {url} was not among the pages searched"}
    if str(year) not in page_year:
        return {"miles": None, "note": f"manufacturer figure rejected: the page is for {page_year}, not {year}"}
    return {"miles": int(miles_raw), "matched_trim": f"{page_year} {trim_name}", "url": url, "note": None}


def range_phrase(miles: int | None, source: str | None) -> str | None:
    if not miles:
        return None
    if source == "manufacturer":
        return f"manufacturer-estimated up to {miles} miles of electric range"
    return f"EPA-estimated up to {miles} miles of electric range"


def _range_info(miles, source, matched_trim, url, flag, cached) -> dict[str, Any]:
    return {
        "miles": miles, "source": source, "phrase": range_phrase(miles, source),
        "matched_trim": matched_trim, "url": url, "flag": flag, "cached": cached,
    }


def electric_range(
    year: int | None,
    make: str | None,
    model: str | None,
    trim: str | None,
    cls: str,
    *,
    body_style: str | None = None,
    extra_text: str = "",
    epa: Callable[..., dict[str, Any]] = epa_lookup,
    manufacturer: Callable[..., dict[str, Any]] = manufacturer_search,
    use_cache: bool = True,
) -> dict[str, Any]:
    """ELECTRIC_RANGE for one vehicle: {"miles", "source", "phrase",
    "matched_trim", "url", "flag", "cached"}. Only plug-in hybrids and
    battery-electric cars are looked up; every other class returns no range
    and no flag without any search."""
    if cls not in PLUG_IN:
        return _range_info(None, None, None, None, None, False)
    if not (year and make and model):
        return _range_info(None, None, None, None, "no year/make/model to look the electric range up — range omitted", False)
    from feature_cache import get_electric_range, save_electric_range

    if use_cache:
        row = get_electric_range(year, make, model, trim)
        if row and row.get("powertrain") == cls:
            if row.get("source") in ("epa", "manufacturer") and row.get("range_miles"):
                return _range_info(row["range_miles"], row["source"], row.get("matched_trim"), row.get("source_url"), None, True)
            return _range_info(None, None, None, None, f"electric range omitted: {row.get('note')}", True)

    def store(miles, source, matched, url, note):
        if use_cache:
            save_electric_range(
                year, make, model, trim, powertrain=cls, range_miles=miles, source=source,
                matched_trim=matched, source_url=url, note=note,
            )

    try:
        res = epa(year, make, model, trim, cls, body_style=body_style, extra_text=extra_text)
    except RangeLookupUnavailable as exc:
        return _range_info(None, None, None, None, f"electric range omitted this run: {exc}", False)
    if res["status"] == "match":
        store(res["miles"], "epa", res["matched_trim"], res["url"], None)
        return _range_info(res["miles"], "epa", res["matched_trim"], res["url"], None, False)
    if res["status"] == "conflict":
        store(None, "conflict", None, None, res["note"])
        return _range_info(None, None, None, None, f"electric range omitted: {res['note']}", False)

    epa_note = res["note"]
    domains = MANUFACTURER_DOMAINS.get(make.lower())
    if not domains:
        note = f"{epa_note}; no manufacturer site on file for {make}"
        store(None, "none", None, None, note)
        return _range_info(None, None, None, None, f"electric range omitted: {note}", False)
    try:
        mf = manufacturer(year, make, model, trim, cls, domains)
    except RangeLookupUnavailable as exc:
        return _range_info(None, None, None, None, f"electric range omitted this run: {epa_note}; {exc}", False)
    if mf.get("miles"):
        store(mf["miles"], "manufacturer", mf.get("matched_trim"), mf.get("url"), epa_note)
        return _range_info(mf["miles"], "manufacturer", mf.get("matched_trim"), mf.get("url"), None, False)
    note = f"{epa_note}; {mf.get('note') or 'no manufacturer figure'}"
    store(None, "none", None, None, note)
    return _range_info(None, None, None, None, f"electric range omitted: {note}", False)


def split_ymm(year_make_model: str | None) -> tuple[int | None, str | None, str | None]:
    m = re.match(r"\s*((?:19|20)\d{2})\s+(\S+)\s*(.*)$", year_make_model or "")
    if not m:
        return None, None, None
    return int(m.group(1)), m.group(2), (m.group(3) or "").strip() or None


def vehicle_powertrain(
    vin: str | None,
    year_make_model: str | None,
    trim: str | None,
    *,
    body_style: str | None = None,
    lookup_range: bool = True,
) -> dict[str, Any]:
    """Class + ELECTRIC_RANGE for one vehicle, from the caches:
    {"class", "label", "source", "signals", "override", "range", "flags"}."""
    sticker_text, recon_text = cached_texts(vin)
    info = classify(vin, year_make_model, trim, sticker_text=sticker_text, recon_text=recon_text)
    year, make, model = split_ymm(year_make_model)
    if lookup_range:
        rng = electric_range(
            year, make, model, trim, info["class"], body_style=body_style,
            extra_text=" ".join(sticker_text[:400].split()),
        )
    else:
        rng = _range_info(None, None, None, None, None, False)
    info["range"] = rng
    info["mild_sentence"] = mild_sentence_for(info, make)
    info["flags"] = [f for f in (info.get("flag"), rng.get("flag")) if f]
    return info


# --------------------------------------------------------------------------- #
# post-generation check
# --------------------------------------------------------------------------- #

RANGE_TOLERANCE_MILES = 1  # a stated range within this of ELECTRIC_RANGE passes
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_RANGE_NUM_RE = re.compile(r"(?<![\d,.$])(\d{2,3})(?:\s*|-)(?:miles?|mi\b\.?)", re.I)
_RANGE_CTX_RE = re.compile(
    r"\brange\b|\b(?:on|per)\s+(?:a\s+)?(?:single\s+|full\s+)?charge\b|\belectric(?:ity|ally)?\b|\bbattery\b|\bEV\b",
    re.I,
)
_LOW_RANGE_RE = re.compile(r"\blow[- ]range\b", re.I)
_ELECTRIC_RANGE_WORDS_RE = re.compile(r"\b(?:all-?|purely\s+)?electric(?:-only)?\s+(?:driving\s+)?range\b", re.I)

_PLUGIN_WORDS = r"\bplug-?in\b|\bPHEV\b"
_BEV_BODY_WORDS = (
    r"\bbattery[- ]electric\b|\b(?:all|fully|purely|100%)[- ]electric\s+"
    r"(?:SUV|sedan|vehicle|car|coupe|crossover|truck|wagon|G-Class|G 580|powertrain)\b|\bzero[- ]emissions?\b"
)
_COMBUSTION_WORDS = (
    r"\bcombustion\b|\bgasoline\b|\bgas[- ](?:engine|tank|mileage|powered)\b|\bfuel\s+(?:tank|stops?)\b"
    r"|\bcylinders?\b|\bV-?(?:6|8|12)\b|\binline[- ](?:three|four|six|3|4|6)\b"
    r"|\b\d\.\d[- ]?(?:liter|litre|L)\b|\bturbocharged\b|\b(?:gas|petrol)\s+engine\b"
)
_ANY_HYBRID_WORD = r"(?<!mild )(?<!mild-)(?<!non-)(?<!non )\bhybrid\b"
_MILD_WORDS = r"\bmild[- ]hybrid\b|\bMHEV\b"
_FULL_HYBRID_WORDS = r"\b(?:standard|conventional|self-charging|full)\s+hybrid\b"

TYPE_RULES: dict[str, list[tuple[str, str]]] = {
    BEV: [
        (_PLUGIN_WORDS, "plug-in wording on a battery-electric car"),
        (_ANY_HYBRID_WORD, "hybrid wording on a battery-electric car"),
        (_COMBUSTION_WORDS, "combustion-engine wording on a battery-electric car"),
    ],
    PHEV: [
        (_BEV_BODY_WORDS, "battery-electric wording on a plug-in hybrid"),
        (_FULL_HYBRID_WORDS, "non-plug-in hybrid wording on a plug-in hybrid"),
    ],
    HYBRID: [
        (_PLUGIN_WORDS, "plug-in wording on a standard hybrid"),
        (_BEV_BODY_WORDS, "battery-electric wording on a standard hybrid"),
    ],
    MILD: [
        (_PLUGIN_WORDS, "plug-in wording on a mild hybrid"),
        (_BEV_BODY_WORDS, "battery-electric wording on a mild hybrid"),
        (_FULL_HYBRID_WORDS, "full-hybrid wording on a mild hybrid"),
    ],
    GAS: [
        (_PLUGIN_WORDS, "plug-in wording on a gas car"),
        (_BEV_BODY_WORDS, "battery-electric wording on a gas car"),
        (_ANY_HYBRID_WORD, "hybrid wording on a gas car"),
    ],
    UNKNOWN: [],
}


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_RE.split(text or "") if s.strip()]


def _ws(s: str) -> str:
    return " ".join((s or "").split()).lower()


def range_claim_numbers(sentence: str) -> list[int]:
    """Mile figures a sentence states as a driving range ([] if none)."""
    s = _LOW_RANGE_RE.sub(" ", sentence)
    nums = [int(n) for n in _RANGE_NUM_RE.findall(s)]
    if nums and _RANGE_CTX_RE.search(s):
        return nums
    return []


def sentence_problems(
    sentence: str, cls: str, rng: dict[str, Any] | None, *, sticker_mild: bool = False
) -> list[str]:
    """Why one sentence breaks the powertrain rules ([] if it doesn't).
    "Mild hybrid" wording passes only on a mild hybrid whose sticker prints
    the term (sticker_mild)."""
    problems: list[str] = []
    rng = rng or {}
    nums = range_claim_numbers(sentence)
    phrase = rng.get("phrase")
    if nums or _ELECTRIC_RANGE_WORDS_RE.search(sentence) and cls not in PLUG_IN:
        if cls not in PLUG_IN:
            problems.append(f"states an electric range on a {CLASS_LABELS[cls]} car")
        elif not phrase:
            problems.append("states an electric range but ELECTRIC_RANGE is omitted")
        elif any(abs(n - rng["miles"]) > RANGE_TOLERANCE_MILES for n in nums):
            problems.append(f"stated range {nums} differs from ELECTRIC_RANGE ({rng['miles']} miles) by more than {RANGE_TOLERANCE_MILES} mile")
        elif rng.get("source") == "manufacturer" and re.search(r"\bEPA\b", sentence):
            problems.append("calls a manufacturer-estimated range EPA")
    for rx, why in TYPE_RULES.get(cls, []):
        if re.search(rx, sentence, re.I):
            problems.append(why)
    if re.search(_MILD_WORDS, sentence, re.I) and not (cls == MILD and sticker_mild):
        problems.append(
            "mild-hybrid wording the sticker does not print" if cls in (MILD, GAS, UNKNOWN)
            else f"mild-hybrid wording on a {CLASS_LABELS[cls]} car"
        )
    return problems


def check_claims(
    text: str, cls: str, rng: dict[str, Any] | None, protected: list[str] | None = None,
    *, sticker_mild: bool = False,
) -> list[tuple[str, list[str]]]:
    """[(sentence, [problems])] for every sentence that breaks the rules.
    Sentences in `protected` (pipeline-built, verbatim) are never flagged,
    and on a mild hybrid neither is MILD_HYBRID_SENTENCE."""
    sources = [p for p in protected or [] if p] + ([MILD_HYBRID_SENTENCE] if cls == MILD else [])
    keep = {_ws(s) for p in sources for s in _sentences(p)}
    out = []
    for s in _sentences(text):
        if _ws(s) in keep:
            continue
        p = sentence_problems(s, cls, rng, sticker_mild=sticker_mild)
        if p:
            out.append((s, p))
    return out


MILD_ONLY_PROBLEMS = {"mild-hybrid wording the sticker does not print"}


def strip_violations(
    text: str, cls: str, rng: dict[str, Any] | None, protected: list[str] | None = None,
    *, sticker_mild: bool = False, keep_mild_only: bool = False,
) -> tuple[str, list[tuple[str, list[str]]]]:
    """Remove every offending sentence, paragraph by paragraph. Returns
    (new text, [(removed sentence, problems)]). With keep_mild_only (existing
    copy, no model call to rewrite it), a sentence whose only problem is
    unprinted mild-hybrid wording stays — it is usually a whole equipment list
    — and is reported with "(kept)" prefixed to its problems."""
    removed: list[tuple[str, list[str]]] = []
    paras = []
    for para in re.split(r"\n\s*\n", text or ""):
        bad = check_claims(para, cls, rng, protected, sticker_mild=sticker_mild)
        if keep_mild_only:
            kept = [(s, p) for s, p in bad if set(p) <= MILD_ONLY_PROBLEMS]
            removed.extend((s, ["(kept) " + x for x in p]) for s, p in kept)
            bad = [b for b in bad if b not in kept]
        if bad:
            drop = {s for s, _ in bad}
            para = " ".join(s for s in _sentences(para) if s not in drop)
            removed.extend(bad)
        if para.strip():
            paras.append(para.strip())
    return "\n\n".join(paras), removed


def data_package_lines(pt: dict[str, Any]) -> list[str]:
    """The POWERTRAIN section of the data package."""
    rng = pt.get("range") or {}
    lines = ["", "=== POWERTRAIN ===", f"POWERTRAIN_CLASS: {pt.get('label') or 'unknown'}"]
    if pt.get("source") == "override":
        lines.append("  (set by a manual override)")
    if pt.get("class") == UNKNOWN:
        lines.append("  Signals disagree: name no powertrain type (no hybrid, plug-in, electric or gas wording).")
    if pt.get("mild_sentence"):
        lines.append(
            "MILD_HYBRID_SENTENCE (use verbatim in paragraph two, right after the engine/powertrain "
            "sentence, or after the opening sentence when there is none; write no other mild-hybrid, "
            "48-volt or EQ Boost wording):"
        )
        lines.append(pt["mild_sentence"])
    elif not (pt.get("class") == MILD and pt.get("sticker_mild")):
        lines.append("  Do not call this vehicle a mild hybrid or present mild-hybrid / MHEV technology as a selling feature (the sticker does not print that term).")
    if pt.get("class") in PLUG_IN and rng.get("phrase"):
        lines.append(f"ELECTRIC_RANGE: {rng['phrase']}")
        lines.append("  (state it only with exactly this phrase; never another range figure)")
    else:
        lines.append("ELECTRIC_RANGE: (omit — state no electric range in any form)")
    return lines


def flags_block(flags: list[str]) -> str:
    return "POWERTRAIN_FLAGS:\n" + "\n".join(f"- {f}" for f in flags)
