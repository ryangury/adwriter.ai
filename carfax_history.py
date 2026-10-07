#!/usr/bin/env python3
"""carfax_history.py — what the Carfax report's own text says about history and
service, deterministically. The text is authoritative; vision only fills a
field the text can't answer.

Read from raw_text (fields are newline-separated in the stored text):

  * every detailed-history row reading "Accident reported[: <severity> damage]"
    or "Damage reported: <severity> damage", with its date and details
  * the header badges ("DAMAGE | Minor Damage", "ACCIDENT | Accident Reported")
  * the summary rows: Accident / Damage, Total Loss, Structural Damage, Airbag
    Deployment, Odometer Check, and the title-brand rows Damage Brands and
    Odometer Brands
  * service rows: date, mileage, facility, and what was done

clean is True only when the text parsed (every summary row found), there is no
accident or damage event and no badge, and every summary row reads its
no-issues wording. The summary row alone is not enough: "Accident / Damage"
reads "No Issues Reported" on some reports whose detailed history lists a
damage event (P51460, PS47294, V23409A on 2026-10-07).

service_ok (the "all service performed at authorized <make> dealers" claim)
needs at least two service records, at least one a real maintenance visit (at
1,000+ miles or described as maintenance — not a pre-delivery inspection, a
delivery-day visit, an emissions test or a bare "inspection performed"), and
every record at a dealer of the vehicle's own make.

Vision (vision_parser.CARFAX_VISION_PROMPT) has no damage field and has
returned 0 accidents for reports with "Accident reported" rows (DT23368B,
P25418, P51460, PM73187, T22750A, V23419A), so it never overrides a field this
module can read — see apply_text_history().
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# Summary row -> the wording that means "nothing reported".
CLEAN_ROW_STATUS: dict[str, set[str]] = {
    "Accident / Damage": {"No Issues Reported"},
    "Total Loss": {"No Issues Reported"},
    "Structural Damage": {"No Issues Reported"},
    "Airbag Deployment": {"No Issues Reported"},
    "Odometer Check": {"No Issues Indicated"},
    "Damage Brands": {"No Problem"},
    "Odometer Brands": {"No Problem"},
}
_TITLE_ROWS = ("Damage Brands", "Odometer Brands")

_ROW_RE = re.compile(r"\|\s*(\d{2}/\d{2}/\d{4})\s*\|\s*([\d,]+|not reported)\s*\|")
_SEVERITY = r"(very minor|minor to moderate|minor|moderate|severe|functional|disabling)"
_EVENT_RE = re.compile(
    rf"Accident reported(?::\s*{_SEVERITY}\s+damage)?|Damage reported:\s*{_SEVERITY}\s+damage", re.I
)
_DETAIL_RE = re.compile(
    r"Involving [^|]+|Vehicle involved in [^|]+|It hit [^|]+|Damage to [^|]+|Vehicle towed|Airbags? (?:did not )?deploy[^|]*",
    re.I,
)
# What a service row did.
_SERVICE_ANY_RE = re.compile(
    r"Vehicle serviced|Pre-delivery inspection|Maintenance inspection|inspection performed|Emissions|"
    r"Oil and filter|Tires?\b|Brakes?\b|Fluid|Filter|Battery|Wheel alignment|Alignment|Recommended maintenance|"
    r"Wiper|Spark plug|Coolant|Transmission (?:serviced|fluid)|Recall performed|Engine",
    re.I,
)
_MAINTENANCE_RE = re.compile(
    r"Maintenance inspection|Oil and filter|Tires? (?:rotated|replaced|mounted|balanced)|Brakes? (?:checked|replaced|serviced)|"
    r"Fluid|Filter (?:replaced|changed)|Air filter|Cabin (?:air )?filter|Battery (?:replaced|checked)|Alignment|"
    r"Recommended maintenance|Wiper|Spark plug|Coolant|Transmission (?:serviced|fluid)|Scheduled maintenance",
    re.I,
)
_NOT_A_VISIT_RE = re.compile(r"Pre-delivery inspection|Emissions|inspection performed|Safety inspection", re.I)
_STATE_CHECK_RE = re.compile(r"Emissions|inspection performed|Safety inspection", re.I)
# A visit within this many days of the first service record (the selling
# dealer's pre-delivery work) is a delivery-day visit, whatever it is called.
_DELIVERY_DAYS = 14
_MAINTENANCE_MILES = 1000

# Dealer-name patterns per make (the facility on each service row).
_MAKE_DEALER_RE = {
    "mercedes-benz": re.compile(r"mercedes|-benz|\bbenz\b|motorcars|\bMB of\b", re.I),
}
# Franchised dealers whose names don't carry the make. Add a name only after
# confirming the store holds that make's franchise.
MAKE_DEALER_ALIASES: dict[str, set[str]] = {
    "mercedes-benz": {"RBM of Atlanta"},  # Mercedes-Benz franchise (rbmofatlanta.com)
}


def _date(mmddyyyy: str) -> date:
    return datetime.strptime(mmddyyyy, "%m/%d/%Y").date()


def _norm(raw_text: str | None) -> str:
    return re.sub(r"\s*\n\s*", " | ", raw_text or "")


def _summary_rows(t: str) -> dict[str, str | None]:
    rows: dict[str, str | None] = {}
    for row in CLEAN_ROW_STATUS:
        if row in _TITLE_ROWS:
            m = re.search(re.escape(row) + r"\s*\|(?:[^|]*\|){0,12}?\s*Guaranteed\s*\|\s*([^|]{2,40}?)\s*\|", t)
        else:
            m = re.search(re.escape(row) + r"\s*\|\s*[^|]*?\s*\|(?:\s*\|)*\s*([^|]{2,40}?)\s*\|", t)
        rows[row] = m.group(1).strip() if m else None
    return rows


def _detailed_rows(t: str):
    i = t.find("Detailed History")
    body = t[i:] if i >= 0 else ""
    ms = list(_ROW_RE.finditer(body))
    for a, b in zip(ms, ms[1:] + [None]):
        yield a.group(1), a.group(2), body[a.end(): b.start() if b else min(len(body), a.end() + 1500)]


_CITY_ST_RE = re.compile(r"^[A-Za-z .'-]+, [A-Z]{2}$")


def _facility(row_text: str) -> str | None:
    """The facility name: Carfax prints "<name> | <City, State> | <City, ST>",
    sometimes after a stray location line, so take the segment two before the
    first "City, ST"; with no such line, the first non-number segment."""
    segs = [x.strip() for x in row_text.split("|") if x.strip()]
    for i, x in enumerate(segs):
        if _CITY_ST_RE.match(x) and i >= 2:
            return segs[i - 2]
    for x in segs:
        if not re.match(r"^[\d,]+$|^not reported$", x, re.I):
            return x
    return None


def dealer_matches(facility: str | None, make: str | None) -> bool:
    """True if a service facility is a dealer of `make` (its name carries the
    make, e.g. "Hendrick Honda", "Mercedes-Benz of Birmingham")."""
    if not facility or not make:
        return False
    key = make.lower()
    if any(facility.lower().startswith(a.lower()) for a in MAKE_DEALER_ALIASES.get(key, ())):
        return True
    rx = _MAKE_DEALER_RE.get(key) or re.compile(r"\b" + re.escape(make.split("-")[0]) + r"\b", re.I)
    return bool(rx.search(facility))


def carfax_history(raw_text: str | None, make: str | None = None) -> dict[str, Any]:
    t = _norm(raw_text)
    rows = _summary_rows(t)
    parsed = bool(t) and all(v is not None for v in rows.values())
    head = t[:3000]
    badge = {
        k: (m.group(1).strip() if m else None)
        for k, m in (("damage", re.search(r"\|\s*DAMAGE\s*\|\s*([^|]{2,40}?)\s*\|", head)),
                     ("accident", re.search(r"\|\s*ACCIDENT\s*\|\s*([^|]{2,40}?)\s*\|", head)))
    }
    events: list[dict[str, Any]] = []
    service: list[dict[str, Any]] = []
    seen = set()
    for d, mi, txt in _detailed_rows(t):
        for m in _EVENT_RE.finditer(txt):
            kind = "accident" if m.group(0).lower().startswith("accident") else "damage"
            sev = (m.group(1) or m.group(2) or "").lower() or None
            if (d, kind) in seen:
                continue
            seen.add((d, kind))
            events.append({"date": d, "kind": kind, "severity": sev,
                           "detail": "; ".join(dict.fromkeys(x.strip() for x in _DETAIL_RE.findall(txt)))})
        if _SERVICE_ANY_RE.search(txt) and not re.search(r"Damage reported|Accident reported", txt, re.I):
            miles = None if mi == "not reported" else int(mi.replace(",", ""))
            maint = bool(_MAINTENANCE_RE.search(txt))
            only_check = bool(_NOT_A_VISIT_RE.search(txt)) and not maint
            # A bare emissions / safety / state-inspection row is not a
            # service visit: it neither counts as a record nor needs to be at
            # a dealer. (A pre-delivery inspection at the selling dealer stays
            # a record, but never the maintenance visit.)
            if only_check and _STATE_CHECK_RE.search(txt) and not re.search(r"Pre-delivery inspection", txt, re.I):
                continue
            fac = _facility(txt)
            if fac and fac.lower().startswith("carfax car care"):
                continue  # Carfax's own maintenance-schedule promo row, not a visit
            service.append({"date": d, "miles": miles, "facility": fac, "maintenance": maint,
                            "only_check": only_check, "dealer": dealer_matches(fac, make)})

    first = min((_date(x["date"]) for x in service), default=None)
    for x in service:
        delivery_day = first is not None and (_date(x["date"]) - first).days <= _DELIVERY_DAYS
        x["qualifies"] = (not x.pop("only_check") and not delivery_day
                          and (x["maintenance"] or (x["miles"] is not None and x["miles"] >= _MAINTENANCE_MILES)))

    reasons: list[str] = []
    if not parsed:
        reasons.append("Carfax summary rows not found in the text — no history claim")
    for e in events:
        reasons.append(f"{e['kind']} reported {e['date']}" + (f" ({e['severity']} damage)" if e["severity"] else ""))
    for k, v in badge.items():
        if v:
            reasons.append(f"header badge {k.upper()}: {v}")
    for row, ok in CLEAN_ROW_STATUS.items():
        if rows.get(row) is not None and rows[row] not in ok:
            reasons.append(f"{row}: {rows[row]}")
    clean = parsed and not events and not any(badge.values()) and all(
        rows[r] in CLEAN_ROW_STATUS[r] for r in CLEAN_ROW_STATUS)

    service_reasons: list[str] = []
    if not parsed:
        service_reasons.append("Carfax text not parsed")
    if len(service) < 2:
        service_reasons.append(f"{len(service)} service record(s); 2 needed")
    if not any(s["qualifies"] for s in service):
        service_reasons.append("no maintenance visit (only pre-delivery / delivery-day / emissions / inspection records)")
    if not make:
        service_reasons.append("make unknown")
    elif service and not all(s["dealer"] for s in service):
        others = sorted({s["facility"] or "?" for s in service if not s["dealer"]})
        service_reasons.append(f"service outside {make} dealers: {', '.join(others)}")
    return {
        "parsed": parsed, "rows": rows, "badge": badge, "events": events,
        "accident_count": sum(1 for e in events if e["kind"] == "accident"),
        "damage_count": sum(1 for e in events if e["kind"] == "damage"),
        "clean": clean, "reasons": reasons,
        "service": service, "service_ok": not service_reasons, "service_reasons": service_reasons,
    }


# --- vision vs text ------------------------------------------------------------ #

# Every Carfax-derived field the vision merge (aggregator._apply_carfax_vision)
# can set, and what this module does with it. "text" = the text value always
# wins when the text has one; vision only fills a field the text left empty.
VISION_FIELDS = {
    "number_of_owners": "text (aggregator._set_owner_count, already text-first)",
    "titled_states": "text (aggregator._set_titled_states, already text-first)",
    "owner_type": "text: the text parser's owner_type is kept; vision fills only when it is empty",
    "no_accidents": "text: carfax_history events (vision had no damage field)",
    "accident_count": "text: carfax_history accident events",
    "accident_severity": "text: worst event severity",
    "no_structural_damage": "text: the Structural Damage summary row",
    "airbag_deployed": "text: the Airbag Deployment summary row",
    "title_brands": "text: the Damage Brands / Odometer Brands title rows",
    "has_disqualifying_event": "text: title brand or airbag from the rows above",
    "all_service_mercedes_benz": "text: service_ok (two records, a maintenance visit, all at the make's dealers)",
    "miles_per_year": "text: the text parser's value is kept; vision fills only when it is empty",
    "service_record_count": "text: the report's 'N Service History Records', else the counted service rows",
    "low_mileage": "text: recomputed from miles_per_year",
}

_SEV_ORDER = ["very minor", "minor", "minor to moderate", "moderate", "severe", "functional", "disabling"]


def apply_text_history(cf: dict[str, Any], make: str | None = None, text_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """`cf` (a Carfax dict, possibly with vision values merged in) with every
    field the text can answer set from the text. text_fields: the text
    parser's own values captured before the vision merge (owner_type,
    miles_per_year, service_record_count), which win over vision's."""
    if not cf or cf.get("error"):
        return cf
    h = carfax_history(cf.get("raw_text"), make)
    cf["carfax_history"] = {k: h[k] for k in ("parsed", "rows", "badge", "events", "accident_count", "damage_count",
                                               "clean", "reasons", "service_ok", "service_reasons")}
    cf["carfax_history"]["service_rows"] = len(h["service"])
    tf = text_fields or {}
    for k in ("owner_type", "miles_per_year", "service_record_count"):
        if tf.get(k) is not None:
            cf[k] = tf[k]
    if not h["parsed"]:
        # No readable text: no history claim, and no flag says otherwise.
        cf["no_accidents"] = None
        cf["clean_history"] = False
        cf["all_service_mercedes_benz"] = False
        return cf
    rows = h["rows"]
    cf["accident_count"] = h["accident_count"]
    cf["damage_count"] = h["damage_count"]
    cf["no_accidents"] = h["accident_count"] == 0 and h["damage_count"] == 0
    sevs = [e["severity"] for e in h["events"] if e["severity"]]
    cf["accident_severity"] = max(sevs, key=lambda s: _SEV_ORDER.index(s)) if sevs else ("minor" if h["events"] else "none")
    cf["no_structural_damage"] = rows["Structural Damage"] in CLEAN_ROW_STATUS["Structural Damage"]
    cf["airbag_deployed"] = rows["Airbag Deployment"] not in CLEAN_ROW_STATUS["Airbag Deployment"]
    cf["no_total_loss"] = rows["Total Loss"] in CLEAN_ROW_STATUS["Total Loss"]
    brands = [f"{r}: {rows[r]}" for r in _TITLE_ROWS if rows[r] not in CLEAN_ROW_STATUS[r]]
    cf["title_brands"] = brands
    cf["has_disqualifying_event"] = bool(brands) or cf["airbag_deployed"]
    cf["clean_history"] = h["clean"]
    cf["all_service_mercedes_benz"] = h["service_ok"]
    m = re.search(r"(\d+)\s+Service History Records?", cf.get("raw_text") or "", re.I)
    cf["service_record_count"] = int(m.group(1)) if m else (cf.get("service_record_count") or len(h["service"]))
    mpy = cf.get("miles_per_year")
    cf["low_mileage"] = bool(mpy) and mpy < 10_000
    return cf


# --- the claims in ad text --------------------------------------------------- #

CLEAN_CLAIM_RE = re.compile(
    r"clean (?:vehicle |carfax |accident )?history|clean carfax|no accidents|accident[- ]free|no reported accidents|"
    r"no accident(?:s)? (?:or damage )?reported|no damage reported",
    re.I,
)
SERVICE_CLAIM_RE = re.compile(
    r"all (?:scheduled )?service (?:has been |was )?(?:performed|completed|done|handled) (?:at|by)|serviced exclusively|"
    r"dealer-maintained",
    re.I,
)


def _sentences(p: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", (p or "").strip()) if s.strip()]


def _upper_first(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


# Deterministic rewrites of the wording the builder and the model use. Each
# returns the sentence without the claim ("" = drop the sentence), or None when
# it doesn't recognise the shape (a hand edit).
def _rewrite_history(s: str, keep_service: bool) -> str | None:
    t = s.strip()
    m = re.match(r"^Clean vehicle history(?: confirmed by Carfax)?[,.]\s*(.*)$", t, re.I)
    if m:
        rest = m.group(1).strip()
        if not rest:
            return ""
        rest = re.sub(r"^with\s+", "", rest, flags=re.I)
        if SERVICE_CLAIM_RE.search(rest) and not keep_service:
            return ""
        return _upper_first(rest if rest.endswith((".", "!", "?")) else rest + ".")
    m = re.search(r",?\s+with (?:a )?clean vehicle history(?: confirmed by Carfax)?(?=[.,])", t, re.I)
    if m:
        return t[: m.start()] + t[m.end():]
    m = re.search(r"\bwith a clean Carfax and ", t, re.I)
    if m:
        return t[: m.start()] + "with " + t[m.end():]
    m = re.match(r"^Carfax shows a clean vehicle history, with (?:this|the) (\S+) averaging (.*)$", t, re.I)
    if m:
        return f"This {m.group(1)} has averaged {m.group(2)}"
    return None


def _rewrite_service(s: str) -> str | None:
    t = s.strip()
    m = re.match(r"^(.*?)[,]?\s*with all service performed at [^.]*?\.$", t, re.I)
    if m:
        lead = m.group(1).strip()
        return (lead + ".") if lead and not lead.endswith(".") else lead
    if re.match(r"^All service (?:has been |was )?performed at [^.]*\.$", t, re.I):
        return ""
    return None


_DANGLING_RE = re.compile(r"^(?:That|This|Such a) (?:clean )?(?:history|record|report)\b|\bthat clean\b|\bthe clean history\b", re.I)


def scrub_claims(text: str, history_clean: bool, service_ok: bool) -> dict[str, Any]:
    """Remove a clean-history claim (when the history isn't clean) and the
    all-service claim (when the records don't support it), deterministically.
    Returns {"text", "removed" [sentences replaced or dropped], "manual"
    [sentences with a claim but no known shape], "dangling" [following
    sentences that may refer back to removed wording]}."""
    paras = (text or "").split("\n\n")
    removed, manual, dangling = [], [], []
    out_paras = []
    for p in paras:
        out = []
        sents = _sentences(p)
        for i, s in enumerate(sents):
            new = s
            hit = False
            if not history_clean and CLEAN_CLAIM_RE.search(new):
                r = _rewrite_history(new, keep_service=service_ok)
                if r is None:
                    manual.append(s)
                    out.append(s)
                    continue
                new, hit = r, True
            if not service_ok and new and SERVICE_CLAIM_RE.search(new):
                r = _rewrite_service(new)
                if r is None:
                    manual.append(s)
                    out.append(s)
                    continue
                new, hit = r, True
            if hit:
                removed.append(s)
                if new:
                    out.append(new)
                nxt = sents[i + 1] if i + 1 < len(sents) else ""
                if nxt and _DANGLING_RE.search(nxt):
                    dangling.append(nxt)
            else:
                out.append(s)
        out_paras.append(" ".join(out))
    return {"text": "\n\n".join(p for p in out_paras if p), "removed": removed, "manual": manual, "dangling": dangling}


def claim_problems(text: str, history_clean: bool, service_ok: bool) -> list[str]:
    """Sentences in `text` making a claim the Carfax doesn't support."""
    bad = []
    for s in _sentences(text.replace("\n\n", " ")):
        if (not history_clean and CLEAN_CLAIM_RE.search(s)) or (not service_ok and SERVICE_CLAIM_RE.search(s)):
            bad.append(s)
    return bad
