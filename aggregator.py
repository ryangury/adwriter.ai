#!/usr/bin/env python3
"""aggregator.py — one call: stock number -> unified data package for Claude.

Runs the four scrapers in sequence (ReconVision, ACV MAX pricing, ACV MAX Carfax,
AutoiPacket), applies the recon include/exclude rules, and returns a single dict
shaped for the ad writer's Claude prompt.

    from aggregator import aggregate
    pkg = aggregate("DT23358A")

If ReconVision has no completed service items *and* its "Close RO" step is still
incomplete, aggregate() returns early with just {"recon_complete": False, ...} so
the caller knows to wait and retry.

CLI:
    python aggregator.py <STOCK#> [--headed] [--fresh-login]
"""

from __future__ import annotations

import argparse
import gc
import json
import re
import sys
from datetime import date, datetime, timezone
from typing import Any

from scraper import (
    ACVMaxScraper,
    AutoiPacketScraper,
    CarfaxError,
    ReconVisionScraper,
    ScraperError,
    StickerNotFoundError,
    WorkOrderLoadError,
    _INTERIOR_MATERIAL_RE,
    _parse_oem_sticker,
)
from vehicle_cache import (
    get_vehicle,
    get_window_sticker,
    get_carfax,
    get_recon,
    get_autoipacket_attempts,
    increment_autoipacket_attempts,
    save_window_sticker,
    save_carfax,
    save_recon,
    needs_window_sticker,
    needs_carfax,
    needs_recon,
)
from vision_parser import parse_carfax_image, parse_recon_image, parse_sticker_image

# The dealer administrative fee added to the ACV Max price to produce the
# legally required advertised/online price. current_price (raw ACV Max) is
# kept as-is everywhere else in the pipeline — reprice detection and
# last_price_at_write must track the ACV Max price, not this fee-inclusive one.
DEALER_DOC_FEE = 899

# --------------------------------------------------------------------------- #
# Recon filtering rules
# --------------------------------------------------------------------------- #

# EXCLUDE — regardless of tier. Never a positive selling signal at any status
# code. (completed:false items are dropped separately, before any of this.)
# NOTE: tint/film-removal was folded in here — it wasn't called out in any
# tier's rules and doesn't read as a confidence signal on any tier either.
_ALWAYS_EXCLUDE = re.compile(
    r"windshield"
    r"|interior trim|trim panel"
    r"|isofix"
    r"|chrome trim"
    r"|\btint\b|film removal"
    r"|refinish",
    re.IGNORECASE,
)

# EXCLUDE — actual transmission repair/replacement/rebuild work. Excluded on
# every tier, including As-Is: "this car needed a new transmission" reads as
# a defect story, not a confidence signal, regardless of price point.
_TRANSMISSION_REPAIR_RE = re.compile(
    r"transmission.{0,20}(?:repair|replace|rebuild)"
    r"|(?:repair|replace|rebuild).{0,20}transmission",
    re.IGNORECASE,
)

# INCLUDE (tier-gated, see _TIER_EXTRA_INCLUDE) — routine transmission fluid
# service, distinct from a repair.
_TRANSMISSION_SERVICE_RE = re.compile(
    r"transmission.{0,20}(?:service|flush|fluid)"
    r"|(?:service|flush|fluid).{0,20}transmission",
    re.IGNORECASE,
)

# Any other transmission mention that is neither clearly a repair nor clearly
# a service — treated as excluded everywhere; ambiguous transmission language
# is never worth the risk of reading as a defect disclosure.
_TRANSMISSION_OTHER_RE = re.compile(r"transmission", re.IGNORECASE)

# Tier-specific EXCLUDE building blocks. See _TIER_EXTRA_EXCLUDE below for how
# these combine per status code.
_CV_BOOT_RE = re.compile(r"cv\s*boot", re.IGNORECASE)
_WHEEL_RIM_RE = re.compile(r"\bwheel\b|\brim\b", re.IGNORECASE)
_BATTERY_RE = re.compile(r"batter(?:y|ies)", re.IGNORECASE)
_ALIGNMENT_RE = re.compile(r"align(?:ment)?", re.IGNORECASE)
_WASH_BUFF_RE = re.compile(r"\bwash\b|\bbuff\b|paint correction", re.IGNORECASE)
_STATE_INSPECTION_RE = re.compile(r"state inspection", re.IGNORECASE)

# EXCLUDE beyond _ALWAYS_EXCLUDE / _TRANSMISSION_REPAIR_RE, keyed by status
# code — MB CPO (10/16) strictest, As-Is (13) loosest. A status code with no
# entry here falls back to the status-10 (strictest) list.
_TIER_EXTRA_EXCLUDE: dict[int, tuple[re.Pattern, ...]] = {
    10: (_CV_BOOT_RE, _WHEEL_RIM_RE, _BATTERY_RE, _ALIGNMENT_RE, _WASH_BUFF_RE, _STATE_INSPECTION_RE),
    16: (_CV_BOOT_RE, _WHEEL_RIM_RE, _BATTERY_RE, _ALIGNMENT_RE, _WASH_BUFF_RE, _STATE_INSPECTION_RE),
    11: (_CV_BOOT_RE, _WHEEL_RIM_RE, _WASH_BUFF_RE, _STATE_INSPECTION_RE),
    12: (_WASH_BUFF_RE, _STATE_INSPECTION_RE),
    13: (_WASH_BUFF_RE,),
}

# INCLUDE (tier-gated) — additions layered on as the tier gets looser.
_COOLANT_FLUSH_RE = re.compile(r"coolant.{0,15}flush|flush.{0,15}coolant", re.IGNORECASE)
_BRAKE_FLUID_FLUSH_RE = re.compile(r"brake\s*fluid", re.IGNORECASE)
_ROTOR_STANDALONE_RE = re.compile(r"rotor", re.IGNORECASE)
_WHEEL_ALL_FOUR_RE = re.compile(
    r"(?:wheel|rim)s?\b.{0,20}\b(?:four|all\s*4|4)\b"
    r"|\b(?:four|all\s*4|4)\b.{0,20}(?:wheel|rim)s?\b",
    re.IGNORECASE,
)
_WHEEL_BEARING_RE = re.compile(r"wheel\s*bearing", re.IGNORECASE)
_SUSPENSION_RE = re.compile(r"control\s*arm|tie\s*rod", re.IGNORECASE)

# Status 13 (As-Is) only — a completed line item with real cost behind it is a
# confidence signal even when it doesn't match a named category above. The
# dollar threshold is a judgment call, not a figure from the spec; tune here.
_MEANINGFUL_INVESTMENT_THRESHOLD = 150

# Tier -> closing phrase for synthesized recon sentences (spark plugs, wiper
# blades, air filter, brakes). Falls back to the status-11 phrasing for any
# unmapped status code.
_TIER_SUFFIX: dict[int, str] = {
    10: "to meet Mercedes-Benz Certified Pre-Owned standards",
    16: "to meet Mercedes-Benz Certified Pre-Owned standards",
    11: "prior to delivery",
    12: "before being offered for sale",
    13: "before being offered for sale. Our team addressed all items identified during inspection",
}


def _tier_suffix(status_code: int) -> str:
    return _TIER_SUFFIX.get(status_code, "prior to delivery")


# INCLUDE — scheduled maintenance service ("A Service" / "B Service" / etc.),
# the generic terms dealers use for the same thing ("Annual Service",
# "Maintenance Service", "Scheduled Maintenance", "Preventive Maintenance"),
# and a bare mileage-interval service mention ("30,000 Mile Service", "60K
# Mile Service") — the Mercedes-Benz A/B classification itself happens later,
# in _classify_mb_service().
_SCHEDULED_SERVICE = re.compile(
    r"\b(?:[ab][\s\-]*service|service[\s\-]*[ab]|maintenance [ab])\b"
    r"|scheduled service|maintenance service"
    r"|scheduled maintenance|annual service|preventive maintenance"
    r"|(?:\d{1,3}[,]?\d{3}|\d+[kK])\s*[mM]ile.{0,20}service"
    r"|service.{0,20}(?:\d{1,3}[,]?\d{3}|\d+[kK])\s*[mM]ile",
    re.IGNORECASE,
)

# INCLUDE (conditionally) — wiper blade replacement. Only reaches the ad when at
# least one other includeable recon item is present; never mentioned alone.
# Blade/insert wording only — a "wiper motor" or "wiper linkage" repair is not a
# selling signal and stays out.
_WIPER_RE = re.compile(r"wiper[\s\-]*blade|wiper[\s\-]*insert", re.IGNORECASE)

# INCLUDE (conditionally) — air filter replacement. Same rule as wiper blades:
# only reaches the ad when at least one other includeable recon item is present.
_AIR_FILTER_RE = re.compile(r"air[\s\-]*filter", re.IGNORECASE)

# INCLUDE (conditionally) — oil/filter change. Same rule as air filter and wiper
# blades: only reaches the ad when at least one other includeable recon item is
# present.
_OIL_CHANGE_RE = re.compile(r"engine oil.{0,20}filter|oil.{0,10}(?:and|&|/).{0,10}filter|\boil change\b", re.IGNORECASE)

# INCLUDE — spark plug replacement. Stands alone: a meaningful enough engine
# service signal to be mentioned even when it is the only recon item.
_SPARK_PLUG_RE = re.compile(r"spark[\s\-]*plug", re.IGNORECASE)

# WORKFLOW STEP — standard RO workflow items that are never recon signals.
# Dropped silently: they appear in neither the included nor the excluded list,
# so the Action Required / Ads Ready emails carry only real recon decisions.
_WORKFLOW_STEP_RE = re.compile(
    r"check in"
    r"|pre-?wash"
    r"|hendrick certified inspection"
    r"|factory cpo inspection"
    r"|parts estimate"
    r"|review estimate"
    r"|parts in stock"
    r"|vehicle ready"
    r"|close ro"
    r"|final quality control"
    r"|visual inspection only"
    r"|recon action item",
    re.IGNORECASE,
)

_SLIM_KEYS = (
    "section",
    "description",
    "labor_hours",
    "labor_cost",
    "parts_cost",
    "total_cost",
    "completion_status",
    "operation_code",
)


def _is_tire_replacement(desc_low: str) -> bool:
    """A tire replacement of some kind: description mentions a tire, but not a
    rotation/balance, and not a TPMS sensor (obviously not new tires)."""
    return (
        "tire" in desc_low
        and "rotation" not in desc_low
        and "balance" not in desc_low
        and not re.search(r"pressure|tpms|sensor|monitor", desc_low)
    )


# A tire-replacement description that names a specific position/count rather
# than the set — "front tire," "one tire," "driver side tire" — is a partial
# replacement, not "all four." Status 13 (As-Is) is the only tier that gets
# credit for a partial replacement; on every other tier it is dropped, since a
# single replaced tire isn't the same confidence signal as a full set.
_PARTIAL_TIRE_INDICATOR_RE = re.compile(
    r"\b(?:one|single|1)\b"
    r"|front\s*(?:left|right|driver|passenger)"
    r"|rear\s*(?:left|right|driver|passenger)"
    r"|driver\s*side|passenger\s*side",
    re.IGNORECASE,
)


def _slim(li: dict[str, Any]) -> dict[str, Any]:
    return {k: li.get(k) for k in _SLIM_KEYS}


def _filter_recon(line_items: list[dict[str, Any]], status_code: int = 10) -> dict[str, Any]:
    """Apply the INCLUDE/EXCLUDE rules for the given tier's status code (10/16
    MB CPO, 11 Hendrick Certified, 12 Hendrick Affordable, 13 As-Is — an
    unmapped code falls back to the status-10 rules, the strictest set).
    Returns the kept line items, the excluded line items (completed items that
    did not make the cut, with a reason), and the summary flags."""
    extra_exclude = _TIER_EXTRA_EXCLUDE.get(status_code, _TIER_EXTRA_EXCLUDE[10])
    suffix = _tier_suffix(status_code)

    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    all_tires_replaced = False
    single_tire_replaced = False
    scheduled_service_done = False
    brake_service_done = False
    brake_front = False
    brake_rear = False
    wiper_front = False
    wiper_rear = False
    wiper_any = False
    air_filter_any = False
    oil_change_any = False
    spark_plugs_replaced = False

    def _drop(li: dict[str, Any], reason: str) -> None:
        excluded.append(
            {
                "section": li.get("section"),
                "description": li.get("description"),
                "reason": reason,
            }
        )

    for li in line_items:
        if not li.get("completed"):
            continue  # incomplete items are "not done", not "excluded"
        d = (li.get("description") or "").lower()

        # Standard RO workflow steps are not recon signals. Drop them before
        # anything else so they never reach the included OR the excluded list.
        if _WORKFLOW_STEP_RE.search(d):
            continue

        if _is_tire_replacement(d):
            if _PARTIAL_TIRE_INDICATOR_RE.search(d):
                if status_code == 13:
                    single_tire_replaced = True
                else:
                    _drop(li, "partial tire replacement — not a full set")
            else:
                all_tires_replaced = True
            continue  # flag only — not added to the line-item list

        # Wiper blades are checked before the EXCLUDE list (a "windshield wiper"
        # line must not be swallowed by the windshield rule). The decision to
        # keep or drop is deferred until we know whether another includeable
        # item is present.
        if _WIPER_RE.search(d):
            wiper_any = True
            if "front" in d:
                wiper_front = True
            if "rear" in d or "back" in d:
                wiper_rear = True
            continue

        # Spark plugs stand alone; air filter is deferred like wiper blades.
        if _SPARK_PLUG_RE.search(d):
            spark_plugs_replaced = True
            continue

        if _AIR_FILTER_RE.search(d) or _OIL_CHANGE_RE.search(d):
            if _AIR_FILTER_RE.search(d):
                air_filter_any = True
            if _OIL_CHANGE_RE.search(d):
                oil_change_any = True
            continue

        # Wheel bearing (As-Is only) must be checked before the tier's wheel/rim
        # exclude rule below, which would otherwise swallow it via bare "wheel".
        if status_code == 13 and _WHEEL_BEARING_RE.search(d):
            kept.append({**_slim(li), "recon_reason": "wheel_bearing"})
            continue

        # Suspension components (As-Is only).
        if status_code == 13 and _SUSPENSION_RE.search(d):
            kept.append({**_slim(li), "recon_reason": "suspension"})
            continue

        # Battery / alignment — includeable starting at Hendrick Certified (11)
        # and every looser tier above it; still excluded on MB CPO (10/16) via
        # _TIER_EXTRA_EXCLUDE. Not excluded for 11+ alone doesn't make these
        # positive — same gap the CV boot check needed fixing for.
        if status_code not in (10, 16) and _BATTERY_RE.search(d):
            kept.append({**_slim(li), "recon_reason": "battery"})
            continue
        if status_code not in (10, 16) and _ALIGNMENT_RE.search(d):
            kept.append({**_slim(li), "recon_reason": "alignment"})
            continue

        # Transmission: an explicit repair/replace/rebuild is never a selling
        # signal, on any tier. Routine service is tier-gated; anything else
        # transmission-related stays excluded rather than guess.
        if _TRANSMISSION_REPAIR_RE.search(d):
            _drop(li, "excluded category")
            continue
        if "transmission" in d:
            if status_code in (12, 13) and _TRANSMISSION_SERVICE_RE.search(d):
                kept.append({**_slim(li), "recon_reason": "transmission_service"})
            else:
                _drop(li, "excluded category")
            continue

        # Coolant / brake fluid flush (Hendrick Affordable and As-Is only).
        if status_code in (12, 13) and _COOLANT_FLUSH_RE.search(d):
            kept.append({**_slim(li), "recon_reason": "coolant_flush"})
            continue
        if status_code in (12, 13) and _BRAKE_FLUID_FLUSH_RE.search(d):
            kept.append({**_slim(li), "recon_reason": "brake_fluid_flush"})
            continue

        # Wheel/rim replacement across a full set (Hendrick Affordable and
        # As-Is only) — a partial swap still falls through to the tier's
        # normal wheel/rim exclusion below.
        if status_code in (12, 13) and _WHEEL_ALL_FOUR_RE.search(d):
            kept.append({**_slim(li), "recon_reason": "wheel_replacement"})
            continue

        # CV boot replacement — not a MB CPO / Hendrick Certified confidence
        # signal, but includeable starting at Hendrick Affordable. Not
        # excluded for 12/13 by _TIER_EXTRA_EXCLUDE, but that alone doesn't
        # make it positive; it needs an explicit INCLUDE like every other
        # category, or it falls through to the generic drop at the bottom.
        if status_code in (12, 13) and _CV_BOOT_RE.search(d):
            kept.append({**_slim(li), "recon_reason": "cv_boot"})
            continue

        if _ALWAYS_EXCLUDE.search(d) or any(p.search(d) for p in extra_exclude):
            _drop(li, "excluded category")
            continue

        if _SCHEDULED_SERVICE.search(d):
            scheduled_service_done = True
            kept.append({**_slim(li), "recon_reason": "scheduled_service"})
            continue

        # Rotor work mentioned without the word "brake" ("standalone rotor
        # replacement") only reads as a brake-confidence signal starting at
        # Hendrick Affordable; on stricter tiers it falls through to the
        # generic drop below.
        is_brake = ("brake" in d and ("pad" in d or "rotor" in d or "service" in d or "inspect" in d)) or (
            status_code in (12, 13) and _ROTOR_STANDALONE_RE.search(d) and "brake" not in d
        )
        if is_brake:
            brake_service_done = True
            if "front" in d:
                brake_front = True
            if "rear" in d or "back" in d:
                brake_rear = True
            continue

        # As-Is only: a completed item with real cost behind it, that didn't
        # match any named category above, is still a confidence signal.
        if status_code == 13:
            cost = li.get("total_cost")
            try:
                cost = float(cost) if cost is not None else None
            except (TypeError, ValueError):
                cost = None
            if cost is not None and cost >= _MEANINGFUL_INVESTMENT_THRESHOLD:
                kept.append({**_slim(li), "recon_reason": "meaningful_investment"})
                continue

        _drop(li, "not a positive selling signal")

    # Spark plugs: a standalone signal — mentioned even when it is the only
    # completed recon item.
    if spark_plugs_replaced:
        spark_li = {k: None for k in _SLIM_KEYS}
        spark_li.update(
            {
                "description": f"Spark plugs replaced {suffix}",
                "completion_status": "completed",
                "recon_reason": "spark_plugs",
            }
        )
        kept.append(spark_li)

    # Single tire replacement (As-Is only) — same standalone treatment as
    # spark plugs, since recon IS the confidence signal on this tier.
    if single_tire_replaced:
        tire_li = {k: None for k in _SLIM_KEYS}
        tire_li.update(
            {
                "description": f"Tire replaced {suffix}",
                "completion_status": "completed",
                "recon_reason": "single_tire",
            }
        )
        kept.append(tire_li)

    # Brakes: combine into one sentence rather than one per line item, and
    # always name front/rear/both when the data supports it. Pads vs. rotors
    # are never distinguished in copy — they're replaced together ~95% of the
    # time, so "brakes replaced" covers it.
    if brake_service_done:
        if brake_front and brake_rear:
            note = "Front and rear brakes replaced"
        elif brake_front:
            note = "Front brakes replaced"
        elif brake_rear:
            note = "Rear brakes replaced"
        else:
            note = "Brakes replaced"
        brake_li = {k: None for k in _SLIM_KEYS}
        brake_li.update(
            {
                "description": f"{note} {suffix}",
                "completion_status": "completed",
                "recon_reason": "brake_service",
            }
        )
        kept.append(brake_li)

    # Wiper blades: include only alongside at least one other positive signal;
    # never on their own.
    wiper_blades_replaced = False
    if wiper_any:
        other_includeable = bool(
            kept or all_tires_replaced or scheduled_service_done or brake_service_done
        )
        if other_includeable:
            wiper_blades_replaced = True
            if wiper_front and wiper_rear:
                note = "Front and rear wiper blades replaced"
            else:
                note = "Wiper blades replaced"
            wiper_li = {k: None for k in _SLIM_KEYS}
            wiper_li.update(
                {
                    "description": f"{note} {suffix}",
                    "completion_status": "completed",
                    "recon_reason": "wiper_blades",
                }
            )
            kept.append(wiper_li)
        else:
            excluded.append(
                {
                    "section": None,
                    "description": "Wiper blade replacement",
                    "reason": "only completed recon item — never mentioned alone",
                }
            )

    # Air filter: same rule as wiper blades — only alongside at least one other
    # positive signal (spark plugs / wipers already sit in `kept` by now).
    air_filter_replaced = False
    if air_filter_any:
        other_includeable = bool(
            kept or all_tires_replaced or scheduled_service_done or brake_service_done
        )
        if other_includeable:
            air_filter_replaced = True
            air_li = {k: None for k in _SLIM_KEYS}
            air_li.update(
                {
                    "description": f"Air filter replaced {suffix}",
                    "completion_status": "completed",
                    "recon_reason": "air_filter",
                }
            )
            kept.append(air_li)
        else:
            excluded.append(
                {
                    "section": None,
                    "description": "Air filter replacement",
                    "reason": "only completed recon item — never mentioned alone",
                }
            )

    # Oil change: same rule as air filter and wiper blades — only counts
    # alongside at least one other positive signal, never as the sole item.
    oil_change_done = False
    if oil_change_any:
        other_includeable = bool(
            kept or all_tires_replaced or scheduled_service_done or brake_service_done
        )
        if other_includeable:
            oil_change_done = True
            oil_li = {k: None for k in _SLIM_KEYS}
            oil_li.update(
                {
                    "description": f"oil and filter changed {suffix}",
                    "completion_status": "completed",
                    "recon_reason": "oil_change",
                }
            )
            kept.append(oil_li)
        else:
            excluded.append(
                {
                    "section": None,
                    "description": "Oil and filter change",
                    "reason": "only completed recon item — never mentioned alone",
                }
            )

    return {
        "line_items": kept,
        "excluded_line_items": excluded,
        "all_tires_replaced": all_tires_replaced,
        "single_tire_replaced": single_tire_replaced,
        "scheduled_service_done": scheduled_service_done,
        "brake_service_done": brake_service_done,
        "wiper_blades_replaced": wiper_blades_replaced,
        "spark_plugs_replaced": spark_plugs_replaced,
        "air_filter_replaced": air_filter_replaced,
        "oil_change_done": oil_change_done,
    }


# --------------------------------------------------------------------------- #
# Per-source shaping
# --------------------------------------------------------------------------- #


def _advertised_price(pr: dict[str, Any]) -> float | None:
    """ACV Max's current_internet_price plus DEALER_DOC_FEE — the legally
    required advertised/online price. None if ACV Max never returned a price
    (never fabricate one)."""
    current = pr.get("current_internet_price")
    if current is None:
        return None
    return current + DEALER_DOC_FEE


# Status codes that represent a certified tier — 10/16 (MB Certified
# Pre-Owned, see MB_CPO_GATE_STATUS_CODES below) and 11 (Hendrick Certified,
# the dealer's own certification program). 12 (Hendrick Affordable) and 13
# (As-Is) are explicitly the tiers reserved for vehicles that do NOT qualify
# for either — see system_prompt_hendrick_affordable.py's own description of
# that program.
#
# This is the ONLY source for vehicle["certified"] in the data package. It
# must never come from anything scraped off the ACV Max pricing/competitive-
# set screen — see scraper.ACVMaxScraper.scrape_pricing()'s docstring on why
# that was unreliable (it read the "Certified" competitive-set filter state,
# not the vehicle's own status, and came back empty/False whenever a
# pre-resolved vehicle_id skipped the inventory-row scrape it depended on —
# which is the common case throughout this file).
CERTIFIED_STATUS_CODES = {10, 11, 16}


def _vehicle(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "stock_number": pr.get("stock_number"),
        "vin": pr.get("vin"),
        "year_make_model": pr.get("year_make_model"),
        "trim_body": pr.get("trim_body"),
        "mileage": pr.get("mileage"),
        # exterior_color / interior_color are overridden by _resolve_colors()
        # in aggregate(); this raw value is the ACV MAX pricing-screen field.
        "exterior_color": pr.get("exterior_color"),
        "certified": pr.get("status_code") in CERTIFIED_STATUS_CODES,
        "status_code": pr.get("status_code"),
        "days_on_lot": pr.get("days_on_lot"),
        # Raw ACV Max price — kept as-is for reprice detection / ad_history's
        # last_price_at_write. Buyer-facing copy and proof points must use
        # advertised_price (below) instead; see DEALER_DOC_FEE.
        "current_price": pr.get("current_internet_price"),
        "advertised_price": _advertised_price(pr),
    }


def _color(val: Any) -> str | None:
    """Trim a raw color string; empty / whitespace -> None."""
    s = str(val).strip() if val is not None else ""
    return s or None


def _interior_from_options(
    option_packages: list[dict[str, Any]] | None,
    added_options_all: list[dict[str, Any]] | None,
) -> str | None:
    """First option / sub-item name that names an interior material
    (Leather / MB-Tex / Nappa). Used as the interior-color fallback when neither
    ACV Max nor the sticker's INTERIOR field carried one."""
    names: list[str] = []
    for o in option_packages or []:
        names.append(o.get("name") or "")
        for si in o.get("sub_items") or []:
            names.append(si.get("name") or "")
    for o in added_options_all or []:
        names.append(o.get("name") or "")
    for name in names:
        if name and _INTERIOR_MATERIAL_RE.search(name):
            return name.strip()
    return None


_INTERIOR_COLOR_WORD_RE = re.compile(
    r"\b(Beige|Tan|Gray|Grey|Brown|Cream|Black|White|Red|Blue)\b", re.IGNORECASE
)
_INTERIOR_LABEL_RE = re.compile(r"\bINTERIOR\b", re.IGNORECASE)
_INTERIOR_WORD_WINDOW = 40


def _interior_from_raw_text(raw_text: str | None) -> str | None:
    """Last-resort interior-color fallback: scan the sticker's raw text for a
    standalone color word (Beige, Tan, Gray, ...) near the word "Interior".
    Non-Mercedes-Benz stickers often describe interior color as a plain color
    word ("Beige") rather than a material name ("Beige Leather"), so neither
    the INTERIOR/SEAT COLOR: line regex nor the Leather/MB-Tex/Nappa material
    scan catches every sticker layout."""
    if not raw_text:
        return None
    for m in _INTERIOR_LABEL_RE.finditer(raw_text):
        start = max(0, m.start() - _INTERIOR_WORD_WINDOW)
        end = m.end() + _INTERIOR_WORD_WINDOW
        cm = _INTERIOR_COLOR_WORD_RE.search(raw_text[start:end])
        if cm:
            return cm.group(1).title()
    return None


def _resolve_colors(
    pricing_raw: dict[str, Any],
    msrp_data: dict[str, Any] | None,
    sticker_raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve exterior/interior color with a fixed source priority.

    Exterior color: AutoiPacket window sticker first, ACV MAX pricing-screen
    color field as the fallback.
    Interior color: AutoiPacket window sticker INTERIOR field first; then, when
    that is empty, the ACV Max pricing-header interior color field (ACV Max
    sometimes shows both exterior and interior color in the vehicle header,
    separately from the AutoiPacket sticker); then a scan of the sticker's
    option_packages / added_options_all for a Leather / MB-Tex / Nappa line
    (MB stickers declare the interior as a paid option, not a header field);
    then, for non-MB stickers whose INTERIOR field uses a plain color word
    that the field/material scans above don't cover, a scan of the sticker's
    raw text for a standalone color word near "Interior". When even that
    finds nothing the color is reported missing and the ad writer flags it
    for human review.

    When msrp_data.source is "acvmax_options_tab" (the MB CPO fallback used
    when AutoiPacket has no sticker at all), the priority flips: those colors
    come from a second visit to ACV Max's own Equipment tab, not a
    manufacturer sticker, so they only fill a gap in the ACV Max pricing
    header rather than overriding it.

    Returns exterior_color / exterior_color_source / interior_color /
    interior_color_source. Source values are "autoipacket",
    "acvmax", "autoipacket_option_line", "autoipacket_raw_text",
    "acvmax_options_tab", or None.
    """
    md = (
        msrp_data
        if isinstance(msrp_data, dict) and not msrp_data.get("error")
        else {}
    )
    sr = sticker_raw if isinstance(sticker_raw, dict) else {}
    from_options_tab = md.get("source") == "acvmax_options_tab"

    acv_ext = _color(pricing_raw.get("exterior_color"))
    sticker_ext = _color(md.get("exterior_color"))
    if from_options_tab:
        if acv_ext:
            ext, ext_src = acv_ext, "acvmax"
        elif sticker_ext:
            ext, ext_src = sticker_ext, "acvmax_options_tab"
        else:
            ext, ext_src = None, None
    elif sticker_ext:
        ext, ext_src = sticker_ext, "autoipacket"
    elif acv_ext:
        ext, ext_src = acv_ext, "acvmax"
    else:
        ext, ext_src = None, None

    sticker_int = _color(md.get("interior_color"))
    acv_int = _color(pricing_raw.get("interior_color"))
    if sticker_int and from_options_tab:
        intr, int_src = sticker_int, "acvmax_options_tab"
        print(f"[aggregator] interior color from equipment tab: {sticker_int}")
    elif sticker_int:
        intr, int_src = sticker_int, "autoipacket"
    elif acv_int:
        intr, int_src = acv_int, "acvmax"
        print(
            f"[aggregator] interior color missing from the sticker; using "
            f"ACV Max pricing header: {acv_int}"
        )
    else:
        intr, int_src = None, None
        guess = _color(
            _interior_from_options(
                md.get("option_packages"), sr.get("added_options_all")
            )
        )
        if guess:
            intr, int_src = guess, "autoipacket_option_line"
            print(
                f"[aggregator] interior color missing from ACV Max and the sticker "
                f"INTERIOR field; using option-line fallback: {guess!r}",
                file=sys.stderr,
            )
        else:
            guess = _interior_from_raw_text(sr.get("raw_text"))
            if guess:
                intr, int_src = guess, "autoipacket_raw_text"
                print(
                    f"[aggregator] interior color missing from all structured "
                    f"sources; using raw sticker text scan: {guess!r}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[aggregator] WARNING: interior color missing for VIN "
                    f"{pricing_raw.get('vin')}",
                    file=sys.stderr,
                )

    return {
        "exterior_color": ext,
        "exterior_color_source": ext_src,
        "interior_color": intr,
        "interior_color_source": int_src,
    }


_MAX_PACKAGE_SUB_ITEMS = 4


def _packages_with_sub_items(
    ap: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Attach a `sub_items` list to every priced option package, taken from the
    unpriced child lines that follow it in `added_options_all`; also return
    `standalone_options`, the unpriced lines that couldn't be reliably
    attributed to any package.

    An OEM window sticker lists each package's contents as unpriced rows indented
    beneath the priced package line. Both sticker parsers keep that order in
    `added_options_all` (priced package line, then its contents, then the next
    priced line), so a package's contents are simply the unpriced entries between
    it and the next priced entry — *when the sticker's layout actually nests
    that way*. Two corrections keep that adjacency heuristic from over-claiming:

    * Self-referential dedup — a sub-item whose name matches its parent
      package's name (case-insensitive) is a sticker-formatting artifact (e.g.
      priced "DC1 - Night Package" immediately followed by an unpriced
      "P55 - Night Package" line on the same sticker), not a real child, and is
      dropped outright rather than surfaced anywhere.
    * Long-run limit — more than _MAX_PACKAGE_SUB_ITEMS unpriced lines between
      one priced line and the next is a sign the sticker's layout doesn't
      actually nest that many lines under one package (real packages have a
      handful of contents, not seven). Those lines are pulled back out into
      `standalone_options` and the package's `sub_items` is left empty rather
      than guessing at a grouping.

    Unpriced lines that never followed any priced package at all (leading
    lines before the first priced entry) are standalone options too — nobody
    is claiming them either.

    This hands the ad writer the real, reliable contents of each package for
    *this* vehicle's sticker, so it never has to reconstruct them from training
    data or a web search. When a package's `sub_items` is empty (no itemised
    breakdown, or the long-run limit tripped), the ad writer may fall back to a
    web search for that package.
    """
    packages = ap.get("option_packages")
    if not packages:
        return packages, []
    added = ap.get("added_options_all") or []

    def _clean(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"code": i.get("code"), "name": i.get("name")} for i in items]

    grouped: list[dict[str, Any]] = []
    standalone: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    run: list[dict[str, Any]] = []

    def _settle_run() -> None:
        """Resolve the pending unpriced `run` into `current`'s sub_items, or
        into `standalone_options` when there's no current package or the run
        is too long to trust."""
        if current is None:
            standalone.extend(_clean(run))
            return
        parent_name = (current.get("name") or "").strip().lower()
        deduped = [
            i for i in run
            if (i.get("name") or "").strip().lower() != parent_name
        ]
        if len(deduped) > _MAX_PACKAGE_SUB_ITEMS:
            standalone.extend(_clean(deduped))
            current["sub_items"] = []
        else:
            current["sub_items"] = _clean(deduped)

    for item in added:
        if item.get("price") is not None:
            _settle_run()
            run = []
            current = {**item, "sub_items": []}
            grouped.append(current)
        else:
            run.append(item)
    _settle_run()

    # `option_packages` is derived from `added_options_all` the same way (the
    # priced entries, in order), so `grouped` should line up one-for-one. If it
    # doesn't — e.g. an OEM-sticker fallback parse that leaves added_options_all
    # empty — keep the bare package list with an empty sub_items on each and no
    # standalone_options (there's no added_options_all to draw them from).
    if len(grouped) != len(packages):
        return [{**p, "sub_items": []} for p in packages], []
    return grouped, standalone


def _sticker_source(ap: dict[str, Any]) -> str:
    """The tier that answered a window-sticker pull (rarity_db_cache /
    ipacket_browse / ipacket_stickerpull / carfax_sticker_link); falls back to
    the render kind for older/simple returns."""
    return ap.get("source") or {
        "html": "autoipacket_html",
        "pdf": "autoipacket_pdf",
    }.get(ap.get("render"), "autoipacket")


def _msrp_data(ap: dict[str, Any] | None) -> dict[str, Any] | None:
    if ap is None:
        return None
    if ap.get("error"):
        return {"error": ap["error"]}
    source = _sticker_source(ap)
    option_packages, standalone_options = _packages_with_sub_items(ap)
    return {
        "source": source,
        "base_price": ap.get("base_price"),
        "option_packages": option_packages,
        # Unpriced added-option lines that _packages_with_sub_items() couldn't
        # reliably attribute to any package (see its docstring) — still real
        # options on the sticker, just not confidently grouped under one.
        "standalone_options": standalone_options,
        "total_msrp": ap.get("total_msrp"),
        "freight": ap.get("freight"),
        # AutoiPacket always carries interior/exterior color on the window
        # sticker; adwriter uses interior_color as the fallback when ACV Max
        # pricing did not include it.
        "interior_color": ap.get("interior_color"),
        "exterior_color": ap.get("exterior_color"),
        # Only populated by the MB CPO options-tab fallback (source
        # "acvmax_options_tab") — the as-equipped package list (with an
        # approximate, non-OEM price per package) scraped in place of a real
        # window sticker. msrp_note flags total_msrp as an approximation when
        # this fallback supplied it.
        "selected_packages": ap.get("selected_packages") or [],
        "msrp_note": ap.get("msrp_note"),
    }


def _apply_sticker_vision(
    msrp_data: dict[str, Any] | None, msrp_raw: dict[str, Any] | None, vin: str | None
) -> dict[str, Any] | None:
    """Vision-parse msrp_raw's sticker image (sticker_image_path, set by
    scraper.AutoiPacketScraper's HTML-iframe or PDF-page-1 capture) and
    overlay the result onto msrp_data, field by field — the same "vision
    primary for what it covers, text parser everywhere else" pattern as
    _apply_carfax_vision().

    Fields the vision prompt's JSON schema doesn't produce (freight,
    selected_packages, msrp_note, source, standalone_options — see
    vision_parser.STICKER_VISION_PROMPT) are left exactly as _msrp_data()
    (the text-parser-driven shaping function) produced them; only
    exterior_color, interior_color, base_price, total_msrp, and
    option_packages come from vision when it succeeds, plus a new
    standard_features field vision alone provides. Returns msrp_data
    unchanged (no vision fields added) if there's no image, no msrp_data to
    begin with (e.g. msrp_raw errored), or the vision call fails.
    """
    if msrp_data is None or msrp_data.get("error"):
        return msrp_data
    image_path = (msrp_raw or {}).get("sticker_image_path")
    if not image_path:
        msrp_data.setdefault("sticker_parse_source", "text_regex")
        return msrp_data

    vjson = parse_sticker_image(image_path, vin)
    if vjson is None:
        msrp_data.setdefault("sticker_parse_source", "text_regex")
        return msrp_data

    if vjson.get("exterior_color"):
        msrp_data["exterior_color"] = vjson["exterior_color"]
    if vjson.get("interior_color"):
        msrp_data["interior_color"] = vjson["interior_color"]
    if vjson.get("base_price") is not None:
        msrp_data["base_price"] = vjson["base_price"]
    if vjson.get("total_msrp") is not None:
        msrp_data["total_msrp"] = vjson["total_msrp"]
    if vjson.get("packages") is not None:
        msrp_data["option_packages"] = [
            {
                "code": None,
                "name": p.get("name"),
                "price": p.get("price"),
                "sub_items": [
                    {"code": None, "name": c} for c in p.get("contents") or []
                ],
            }
            for p in vjson["packages"]
        ]
    if vjson.get("standard_features") is not None:
        msrp_data["standard_features"] = vjson["standard_features"]
    msrp_data["sticker_parse_source"] = "vision"
    return msrp_data


# Micro-adjustment / syndication-feed line items — VIN and plate paperwork,
# dispatch/sea-packing notes, generic manual/floormat/first-aid/triangle
# omissions, USA-spec and English-inscription notices, and the like. These
# come straight through from the OEM sticker feed and read as raw syndication
# dump rather than curated equipment copy, so _filter_feature_noise() drops
# any sub_item / standalone_option whose name contains one of them before
# format_data_package() ever sees it.
SUPPRESS_FEATURES = [
    "thigh support",
    "double sun visor",
    "license plate",
    "omission of",
    "pre-installation",
    "identification label",
    "vin code",
    "sea packing",
    "dispatch",
    "credit for missing",
    "carpeted floor mat",  # suppresses generic carpeted mats; leaves AMG/Designo
                           # branded floor mat options intact
    "floor mats, front",   # catches "Floor Mats, Front and Rear" variants
    "floor mats, rear",
    "owners manual",
    "first-aid kit omission",
    "warning triangle omission",
    "english inscriptions",
    "usa specification",
    "technical modifications",
    "vin number",
    "ecall",
    "tire pressure monitoring",
    "seat weight sensing",
    "rear belt status",
    "communication module",
    "twin cupholders",
    "fingerprint scanner",
    "anti-theft alarm",
    "garage door opener",
    "storage space package",
    "large capacity fuel tank",
    "electronic trunk closer",
    "independent trunk locking",
    "first-aid kit",
    "warning triangle",
    "shift-by-wire",
    "electric parking brake",
    "power windows",
    "power door locks",
    "intermittent wipers",
    "carpeted cargo area",
    "cargo net",
    "cargo cover",
]


def _is_noise_feature(name: str | None) -> bool:
    low = (name or "").lower()
    return any(term in low for term in SUPPRESS_FEATURES)


def _filter_feature_noise(msrp_data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop SUPPRESS_FEATURES matches (case-insensitive substring, against
    each item's `name`) from every option package's `sub_items` and from
    `standalone_options` — run after both the text-parser and vision-parser
    paths (_msrp_data() / _apply_sticker_vision()) so neither can reintroduce
    noise the other already dropped."""
    if not msrp_data or msrp_data.get("error"):
        return msrp_data
    for package in msrp_data.get("option_packages") or []:
        package["sub_items"] = [
            i for i in package.get("sub_items") or [] if not _is_noise_feature(i.get("name"))
        ]
    msrp_data["standalone_options"] = [
        i for i in msrp_data.get("standalone_options") or [] if not _is_noise_feature(i.get("name"))
    ]
    return msrp_data


def _recalc_proof_point(p: dict[str, Any], current_price: float | None) -> dict[str, Any]:
    """Recompute one proof point's gap/direction against current_price — the
    raw, pre-fee ACV Max internet price — instead of the raw ACV Max price
    ACV Max itself compared against when it rendered the scraped gap. ACV
    Max's own benchmark prices are already computed against that raw price,
    so comparing them against advertised_price (current_price + admin_fee)
    would double-count the fee and understate every favorable gap by
    admin_fee — see build_proof_point_sentence()'s docstring, which this
    mirrors so pkg["pricing"]'s primary_proof_point/secondary_proof_point/
    all_proof_points agree with pkg["proof_point_sentence"]. Left unchanged
    (stale, ACV-Max-price-based) when there's no benchmark_price or no
    current_price to recompute against."""
    q = dict(p)
    benchmark = q.get("benchmark_price")
    if benchmark is None or current_price is None:
        return q
    if current_price < benchmark:
        q["direction"] = "below"
        q["gap"] = benchmark - current_price
    elif current_price > benchmark:
        q["direction"] = "above"
        q["gap"] = current_price - benchmark
    else:
        q["direction"] = "at"
        q["gap"] = 0
    label = q.get("label") or q.get("key") or "benchmark"
    q["summary"] = (
        f"current price matches {label}"
        if q["direction"] == "at"
        else f"current price is ${q['gap']:,.0f} {q['direction']} {label}"
    )
    return q


# Proof-point keys, per scraper._proof_point_key() (which derives them from
# ACV Max's own on-page label text, not a guess): "typical_listing_price" is
# the generic, non-JD-Power-branded "Typical Listing Price" benchmark ACV Max
# shows — Kelley Blue Book's own product of that exact name — while
# "jd_power" is the separately JD Power-branded benchmark. Both can appear on
# the same vehicle's pricing screen; _select_proof_points() below decides
# between them. "market_average" and "acv_max_retail" are as named.
_KBB_PROOF_POINT_KEY = "typical_listing_price"
_JD_POWER_PROOF_POINT_KEY = "jd_power"
_MARKET_PROOF_POINT_KEY = "market_average"

_BOOK_VALUE_LABELS = {
    _KBB_PROOF_POINT_KEY: "Kelley Blue Book Typical Listing Price",
    _JD_POWER_PROOF_POINT_KEY: "J.D. Power Typical Listing Price",
}
_MARKET_LABEL = "active regional market listings"


def _best_by_key(recalced: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    """The most favorable (largest gap) recalculated proof point with this
    key that's actually favorable (direction == 'below'), or None if there
    isn't one."""
    candidates = [
        p for p in recalced if p.get("key") == key and p.get("direction") == "below"
    ]
    return max(candidates, key=lambda p: p.get("gap") or 0) if candidates else None


def _select_proof_points(recalced: list[dict[str, Any]]) -> dict[str, Any]:
    """Decide the primary and secondary proof-point anchors for the ad — the
    PROOF POINT DECISION TREE described in SYSTEM_PROMPT's PRICING PROOF
    POINT RULES.

    This MUST match build_proof_point_sentence()'s actual selection logic
    exactly — that function writes the real ad sentence, this one only
    drives the review dashboard's Primary/Secondary badges. The two used to
    disagree (this function had no dollar floor and strictly preferred
    Typical Listing Price regardless of gap size; build_proof_point_sentence
    requires a $1,000 floor and picks the larger favorable gap between J.D.
    Power and Typical Listing Price). Confirmed via live ad testing that the
    old version showed the wrong benchmark as "Primary" on the dashboard
    while the ad correctly used the larger, doc-compliant gap. Do not let
    these drift apart again — any change to one's selection rule must be
    mirrored in the other, or ideally both should eventually call one
    shared function.

    1. Primary (book value) — whichever of J.D. Power / Typical Listing
       Price ("KBB" internally) has the LARGER favorable gap, but only if
       that gap clears _PROOF_POINT_FAVORABLE_THRESHOLD ($1,000). Max-gap-
       wins, not KBB-always-wins.
    2. Secondary (market reality) — the market-average benchmark, only when
       it independently clears the same $1,000 floor.
    3. If neither book value clears the floor but market does, market is
       promoted into the primary slot instead, so a single available anchor
       always lands in primary_proof_point.

    ACV Max Retail is never selected here — never buyer-facing.
    """
    def _favorable_by_key(key: str) -> dict[str, Any] | None:
        p = _best_by_key(recalced, key)
        if p is None or (p.get("gap") or 0) <= _PROOF_POINT_FAVORABLE_THRESHOLD:
            return None
        return p

    jdp = _favorable_by_key(_JD_POWER_PROOF_POINT_KEY)
    kbb = _favorable_by_key(_KBB_PROOF_POINT_KEY)
    market = _favorable_by_key(_MARKET_PROOF_POINT_KEY)

    def _anchor(p: dict[str, Any], label: str) -> dict[str, Any]:
        return {"label": label, "gap": p.get("gap"), "direction": p.get("direction")}

    book_candidates = []
    if jdp is not None:
        book_candidates.append((jdp, _BOOK_VALUE_LABELS[_JD_POWER_PROOF_POINT_KEY]))
    if kbb is not None:
        book_candidates.append((kbb, _BOOK_VALUE_LABELS[_KBB_PROOF_POINT_KEY]))

    book, book_label = (
        max(book_candidates, key=lambda t: t[0].get("gap") or 0)
        if book_candidates
        else (None, None)
    )

    if book is not None:
        primary = _anchor(book, book_label)
        secondary = _anchor(market, _MARKET_LABEL) if market is not None else None
    elif market is not None:
        primary = _anchor(market, _MARKET_LABEL)
        secondary = None
    else:
        primary = None
        secondary = None

    return {
        "primary_proof_point": primary,
        "secondary_proof_point": secondary,
        "proof_points_count": sum(1 for p in (primary, secondary) if p is not None),
    }


def _pricing(
    proof_points: list[dict[str, Any]], current_price: float | None
) -> dict[str, Any]:
    """Recalculate every proof point's gap/direction against current_price
    (the raw, pre-fee ACV Max internet price — never advertised_price, which
    would double-count DEALER_DOC_FEE against benchmarks ACV Max already
    computed pre-fee; see _recalc_proof_point()), keep only the ones BELOW
    benchmark (a favorable story), and flag the one with the largest gap.

    primary_proof_point / secondary_proof_point / proof_points_count (from
    _select_proof_points()) are the two-anchor-maximum selection the ad
    itself must use — see PRICING PROOF POINT RULES. proof_points_below /
    best_proof_point / all_proof_points are kept for older consumers
    (the reprice flow, the daily email) that still want the full favorable
    list or a plain largest-gap pick rather than the book/market-anchored
    selection.
    """
    recalced = [_recalc_proof_point(p, current_price) for p in proof_points]
    below = sorted(
        (p for p in recalced if p.get("direction") == "below"),
        key=lambda p: p.get("gap") or 0,
        reverse=True,
    )
    best = below[0] if below else None
    out = []
    for p in below:
        q = dict(p)
        q["is_best"] = p is best
        out.append(q)
    return {
        "proof_points_below": out,
        "best_proof_point": best,
        # Every proof point, favorable or not — the daily email prints them all
        # (J.D. Power / Market Average / ACV Max Retail) with a per-line verdict.
        "all_proof_points": recalced,
        **_select_proof_points(recalced),
    }


def _market_velocity(pr: dict[str, Any]) -> dict[str, Any]:
    """Market Days Supply / Competitive Set / Rank fields from the ACV MAX
    pricing screen. All fields are None when ACV MAX didn't show that widget
    for this vehicle — never fabricated."""
    return {
        "overall_market_days": pr.get("overall_market_days"),
        "matching_market_days": pr.get("matching_market_days"),
        "market_velocity_gap": pr.get("market_velocity_gap"),
        "overall_count": pr.get("overall_count"),
        "matching_count": pr.get("matching_count"),
        "search_distance": pr.get("search_distance"),
        "market_rank": pr.get("market_rank"),
        "market_rank_of": pr.get("market_rank_of"),
    }


def _carfax(cf: dict[str, Any] | None) -> dict[str, Any] | None:
    if cf is None:
        return None
    return {k: v for k, v in cf.items() if k != "raw_text"}


# vision JSON owner_type -> the values format_data_package()/_ads_ready_full_block()
# in adwriter.py already know how to render (see _parse_carfax()'s owner_type
# derivation in scraper.py — this mirrors that vocabulary).
_VISION_OWNER_TYPE_MAP = {
    "personal": "personal use",
    "lease": "personal lease",
    "corporate": "fleet",
}


def _owner_count_from_raw_text(raw_text: str | None) -> int | None:
    """Deterministic owner count read straight from the report's own curated
    summary line. Proven 22/22 correct against manual verification, vs. the
    vision-parsed 'owners' field's demonstrated unreliability on this
    specific vehicle (including fabricating quoted evidence for a wrong
    count). This is the source of truth for owner count going forward —
    vision's own 'owners' field is only a fallback when neither pattern
    below is found anywhere in the raw text."""
    if not raw_text:
        return None
    if re.search(r"CARFAX\s+1[\s-]*Owner\s+Vehicle", raw_text, re.IGNORECASE):
        return 1
    m = re.search(r"(\d+)\s+Previous\s+Owners?\b", raw_text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _set_owner_count(
    carfax_raw: dict[str, Any], vision_count: int | None, vin: str | None
) -> None:
    """Set carfax_raw["number_of_owners"]: the text-pattern count when the raw
    text has one (logging any disagreement with vision), else vision's count,
    else whatever value the text parser already left there."""
    deterministic_count = _owner_count_from_raw_text(carfax_raw.get("raw_text"))
    if deterministic_count is not None:
        if vision_count is not None and vision_count != deterministic_count:
            print(
                f"[carfax] owner count disagreement for {vin}: vision said "
                f"{vision_count}, text pattern says {deterministic_count} — "
                f"using text pattern"
            )
        carfax_raw["number_of_owners"] = deterministic_count
    elif vision_count is not None:
        carfax_raw["number_of_owners"] = vision_count  # fallback: no pattern matched


def _apply_carfax_vision(
    carfax_raw: dict[str, Any], vin: str | None
) -> dict[str, Any]:
    """Vision-parse carfax_raw's screenshot (carfax_image_path, set by
    scraper.ACVMaxScraper._capture_carfax_screenshot()) and overlay the
    result onto carfax_raw, field by field, so vision becomes the primary
    source for what it covers.

    This is a field-by-field OVERLAY onto the existing regex-parsed dict, not
    a wholesale replacement — carfax_raw already carries fields the vision
    prompt's JSON schema doesn't ask for at all (raw_text, warranty_status /
    warranty_claimable and friends from the odometer cross-check in
    scrape_carfax(), window_sticker_url, carfax_open_path, ...), and those
    stay exactly as the text parser produced them. Returns carfax_raw
    unchanged if there's no image path, the image is missing, or the vision
    call fails — the existing text parser's output is always a complete,
    valid result on its own.
    """
    image_path = carfax_raw.get("carfax_image_path")
    if not image_path:
        _set_owner_count(carfax_raw, None, vin)
        carfax_raw.setdefault("carfax_parse_source", "text_regex")
        return carfax_raw

    vjson = parse_carfax_image(image_path, vin)
    if vjson is None:
        _set_owner_count(carfax_raw, None, vin)
        carfax_raw.setdefault("carfax_parse_source", "text_regex")
        return carfax_raw

    owner_type = _VISION_OWNER_TYPE_MAP.get(vjson.get("owner_type"), carfax_raw.get("owner_type"))
    accident_count = vjson.get("accident_count")
    title_brands = vjson.get("title_brands") or []
    airbag_deployed = bool(vjson.get("airbag_deployed"))

    # Owner count: deterministic text pattern first, vision only as a fallback.
    _set_owner_count(carfax_raw, vjson.get("owners"), vin)
    # NOT trustworthy as evidence: the vision model has been shown to invent a
    # quoted "2 Previous Owners" for reports that say "CARFAX 1-Owner Vehicle".
    # Kept only as a raw record of what vision claimed; nothing may present it
    # as a citation or audit justification.
    carfax_raw["owners_evidence"] = vjson.get("owners_evidence")
    carfax_raw["owner_type"] = owner_type
    carfax_raw["accident_severity"] = vjson.get("accident_severity")
    if isinstance(accident_count, int):
        carfax_raw["no_accidents"] = accident_count == 0
        # Persisted (the text-regex parser has no equivalent) so
        # build_carfax_sentence() can state a specific accident count instead
        # of just the no_accidents boolean.
        carfax_raw["accident_count"] = accident_count
    if "structural_damage" in vjson:
        carfax_raw["no_structural_damage"] = not bool(vjson["structural_damage"])
    carfax_raw["title_brands"] = title_brands
    carfax_raw["airbag_deployed"] = airbag_deployed
    # Activates _carfax_disqualifying_gate() in aggregate() below — the text
    # parser never set this key at all (title_brands/airbag_deployed aren't
    # part of its schema), so that gate has never actually fired until now.
    carfax_raw["has_disqualifying_event"] = bool(title_brands) or airbag_deployed
    # "authorized dealer" here means the vehicle's OWN brand, which is the
    # correct concept for the Hendrick Certified/Affordable/As-Is programs'
    # non-MB inventory — the regex equivalent (all_service_mercedes_benz) only
    # ever matches Mercedes-Benz facility names, so it was never true for a
    # non-MB vehicle regardless of how good that vehicle's service history was.
    if "all_service_authorized_dealer" in vjson:
        carfax_raw["all_service_mercedes_benz"] = bool(vjson["all_service_authorized_dealer"])
    if vjson.get("geographic_states"):
        carfax_raw["titled_states"] = vjson["geographic_states"]
    if vjson.get("annual_mileage") is not None:
        carfax_raw["miles_per_year"] = vjson["annual_mileage"]
    if vjson.get("service_record_count") is not None:
        carfax_raw["service_record_count"] = vjson["service_record_count"]
    if "low_mileage" in vjson:
        carfax_raw["low_mileage"] = bool(vjson["low_mileage"])
    carfax_raw["carfax_parse_source"] = "vision"

    if carfax_raw.get("no_structural_damage") is False:
        print(
            f"[vision] WARNING: structural damage flagged (vision) for VIN "
            f"{vin or 'unknown'} — verify against the report image before "
            f"treating this as final",
            file=sys.stderr,
        )
    return carfax_raw


def _recon_vision_item_to_normalized(li: dict[str, Any]) -> dict[str, Any]:
    """One vision-parsed recon line item -> the same shape
    ReconVisionScraper._normalize_line_item() produces, so _filter_recon()
    and build_recon_sentence() don't need to know which parser produced it.
    `kind` is hardcoded to "service": the vision prompt is already scoped to
    "actual service work" and explicitly excludes workflow/administrative
    tasks, so unlike the raw DOM scrape (which includes every row, workflow
    steps included), every item vision returns IS a service-kind item."""
    completed = bool(li.get("completed"))
    return {
        "service_id": None,
        "section": li.get("department"),
        "kind": "service",
        "description": li.get("description"),
        "labor_hours": li.get("labor_hours"),
        "labor_cost": li.get("labor"),
        "parts_cost": li.get("parts"),
        "total_cost": li.get("total"),
        "completion_status": "TASK COMPLETED" if completed else "INCOMPLETE",
        "completed": completed,
        "rejected": None,
        "repair_order": None,
        "operation_code": None,
    }


def _apply_recon_vision(
    recon_raw: dict[str, Any], vin: str | None
) -> dict[str, Any]:
    """Vision-parse recon_raw's screenshot (recon_image_path, set by
    scraper.ReconVisionScraper._capture_recon_screenshot()) and overlay the
    result onto recon_raw's line_items, so vision becomes the primary source
    for line-item extraction — the raw DOM-walker/regex parse is fragile
    about labor/parts/total splits and completion glyphs. Same overlay
    pattern as _apply_carfax_vision(): only `line_items` (and
    `line_item_count`) are replaced; every other recon_raw field
    (work_order_id, sections, section_totals, vin, raw_text,
    recon_image_path, ...) is left exactly as scrape_work_order() produced
    it. Returns recon_raw unchanged if there's no image path, the image is
    missing, or the vision call fails.

    Deliberately NOT called before _recon_is_complete() decides
    recon_complete — see aggregate()'s ReconVision step. The vision prompt
    excludes workflow/administrative rows (Close RO among them), so
    vision-only line_items can't answer "is the Close RO step done yet?";
    completeness must stay decided from the raw scrape's full item list.
    """
    image_path = recon_raw.get("recon_image_path")
    if not image_path:
        recon_raw.setdefault("recon_parse_source", "dom_regex")
        return recon_raw

    vjson = parse_recon_image(image_path, vin)
    if vjson is None:
        recon_raw.setdefault("recon_parse_source", "dom_regex")
        return recon_raw

    vision_items = [
        _recon_vision_item_to_normalized(li) for li in vjson.get("line_items") or []
    ]
    recon_raw["line_items"] = vision_items
    recon_raw["line_item_count"] = len(vision_items)
    recon_raw["recon_parse_source"] = "vision"
    return recon_raw


def _capture_sticker_to_rarity(
    vin: str,
    msrp_raw: dict[str, Any] | None,
    pricing_raw: dict[str, Any],
    *,
    source: str = "ad_write_capture",
) -> None:
    """Opportunistically fold a successful ad-write sticker pull into rarity.db
    (same schema as vin_importer). Silent and best-effort — never disturbs the
    aggregate() result. No-op if the VIN is already stored as 'complete'."""
    if not msrp_raw or msrp_raw.get("error"):
        return
    try:
        from vin_importer import _parse_description, capture_sticker, is_mercedes_make

        yr, mk, md, tr = _parse_description(
            pricing_raw.get("year_make_model") or msrp_raw.get("year_make_model")
        )
        if not is_mercedes_make(mk):
            return  # rarity database is Mercedes-Benz only — never capture a non-MB sticker here, regardless of how it was sourced (AutoiPacket, Carfax link, or ACV Max options tab)
        # status_code only — never pricing_raw.get("certified"), which came
        # from the ACV Max competitive-set screen's own "Certified" filter
        # state, not the vehicle's own status (see CERTIFIED_STATUS_CODES).
        status_code = pricing_raw.get("status_code")
        certified = status_code in CERTIFIED_STATUS_CODES if status_code is not None else None
        context = {
            "year": yr,
            "make": mk,
            "model": md,
            "trim": pricing_raw.get("trim_body") or tr,
            "exterior_color": pricing_raw.get("exterior_color")
            or msrp_raw.get("exterior_color"),
            "mileage": pricing_raw.get("mileage"),
            "certified": (
                1 if certified else (0 if certified is not None else None)
            ),
            "price": pricing_raw.get("current_internet_price"),
        }
        capture_sticker(vin, msrp_raw, context=context, source=source)
    except Exception:  # noqa: BLE001 - corpus capture must never break an ad write
        pass


def _make_from_ymm(year_make_model: str | None) -> str | None:
    """'2021 Ford F-150 XLT' -> 'Ford'. Best-effort, used only to route
    _parse_oem_sticker() to the right format-specific parser."""
    if not year_make_model:
        return None
    m = re.match(r"\s*(?:19|20)\d{2}\s+(\S+)", year_make_model)
    return m.group(1) if m else None


def _is_z_stock(stock_number: str | None) -> bool:
    """True when a stock number is a Z stock (former Mercedes-Benz of Durham
    courtesy/loaner vehicle) that qualifies for Z stock ad treatment.

    A vehicle is Z stock when BOTH hold:
      * the stock number starts with the letter Z, and
      * the stock number does NOT end in a letter.

    The first rule covers ZT, ZV, ZW and any future loaner formats. The second
    rule excludes trade-ins: a trailing letter (e.g. ZT22867A) marks a trade-in
    against the loaner deal, which is handled by the standard trailing-letter
    trade-in logic, not Z stock treatment.
    """
    s = (stock_number or "").strip().lstrip("#").upper()
    if not s:
        return False
    return s[0] == "Z" and not s[-1].isalpha()


# --------------------------------------------------------------------------- #
# Provenance sentence — paragraph one, sentence two
# --------------------------------------------------------------------------- #


def build_provenance_sentence(
    stock_number: str | None,
    carfax_data: dict[str, Any] | None,
    status_code: int | None,
    clean_history: bool | None = None,
) -> str:
    """Pre-written provenance sentence for paragraph one sentence two, decoded
    from the stock number prefix/suffix, Carfax data, and ACV Max status code
    (see STOCK NUMBER AND PROVENANCE RULES in SYSTEM_PROMPT). Claude uses this
    verbatim instead of decoding the stock number itself.

    `clean_history` defaults to carfax_data["no_accidents"] is True when not
    given explicitly — the trailing-letter (trade-in) branch folds a clean
    Carfax into this sentence directly, so build_carfax_sentence() (passed
    this sentence as its own provenance_sentence argument) knows attribution
    is already stated and returns None instead of repeating it.

    Note: carfax_data is the raw Carfax dict (carfax_raw), whose owner-count
    field is "number_of_owners", not "owners".
    """
    cf = carfax_data if isinstance(carfax_data, dict) else {}
    owners = cf.get("number_of_owners") or 1
    owner_type = (cf.get("owner_type") or "").lower()
    s = (stock_number or "").strip().lstrip("#").upper()
    if clean_history is None:
        clean_history = cf.get("no_accidents") is True

    # Layer 1 — status code override: Z stock loaner. Ignore everything else.
    if status_code == 16:
        return (
            "Former Mercedes-Benz of Durham courtesy vehicle, never titled to "
            "a private owner. The buyer of this vehicle will be the first "
            "owner on record, eligible for first-owner financing through "
            "Mercedes-Benz Financial Services. Used briefly in our courtesy "
            "fleet and professionally detailed before delivery, this vehicle "
            "carries more warranty coverage than a comparable new purchase. "
            "MB CPO certification adds a full year of unlimited-mile coverage "
            "on top of the remaining factory warranty, typically resulting in "
            "more total coverage and thousands less than buying new."
        )

    # Layer 2 — trailing letter = trade-in against the preceding stock number.
    if s and s[-1].isalpha():
        if clean_history:
            return (
                f"Local trade-in, {owners} owner{'s' if owners > 1 else ''}, "
                "personal use with clean vehicle history confirmed by Carfax."
            )
        return (
            f"Local trade-in, {owners} owner{'s' if owners > 1 else ''}, "
            "personal use confirmed by Carfax."
        )

    # Layer 3 — stock prefix decode. Two-letter prefixes are checked before
    # the generic single-letter "P" they would otherwise also match.
    if s.startswith("PM"):
        if "lease" in owner_type:
            return "One owner, off-lease, personal use."
        return "One owner, personal use."

    if s.startswith("PS"):
        return "One owner, local private purchase, personal use confirmed by Carfax."

    if s.startswith("X"):
        return (
            f"{owners} owner{'s' if owners > 1 else ''}, "
            "personal use confirmed by Carfax."
        )

    if s.startswith("P"):
        if owners == 1:
            return "One owner, personal use confirmed by Carfax."
        return f"{owners} owners, personal use confirmed by Carfax."

    # Default — unknown prefix.
    return (
        f"{owners} owner{'s' if owners > 1 else ''}, "
        "personal use confirmed by Carfax."
    )


_ACCIDENT_SEVERITY_MAP = {"minor": "minor", "moderate": "moderate", "severe": "significant"}
_ACCIDENT_COUNT_WORDS = {1: "one", 2: "two", 3: "three"}


def build_carfax_sentence(
    carfax_data: dict[str, Any] | None,
    status_code: int | None,
    provenance_sentence: str | None = None,
) -> str | None:
    """Pre-written sentence for paragraph one sentence three, built from
    Carfax accident/service/mileage signals. Returns None when the sentence
    should be omitted (Z stock, no Carfax data, unknown accident history, or
    a disqualifying title/airbag event — never disclosed per WHAT NEVER
    APPEARS IN COPY / the recon exclusion rules).

    `provenance_sentence` is the already-built PROVENANCE SENTENCE (sentence
    two) — when it already mentions Carfax (see build_provenance_sentence()'s
    trailing-letter/clean_history branch), this drops the "confirmed by
    Carfax"/"reported on Carfax" attribution here so it isn't stated twice
    across sentences two and three.

    Note: carfax_data is the raw Carfax dict (carfax_raw). Its field names
    differ from the vision JSON's own vocabulary — "all_service_mercedes_benz"
    not "all_service_authorized_dealer", "miles_per_year" not
    "annual_mileage", "titled_states" not "geographic_states". "accident_count"
    is only present when the vision parser ran (see _apply_carfax_vision) —
    the text-regex parser only ever produces the "no_accidents" boolean.
    """
    if status_code == 16:
        return None

    cf = carfax_data if isinstance(carfax_data, dict) else {}
    if not cf:
        return None

    no_accidents = cf.get("no_accidents")
    if no_accidents is None:
        return None

    provenance_has_carfax = "carfax" in str(provenance_sentence or "").lower()
    provenance_has_clean_history = "clean vehicle history" in str(provenance_sentence or "").lower()
    all_service_mb = cf.get("all_service_mercedes_benz", False)
    miles_per_year = cf.get("miles_per_year") or 0

    if no_accidents:
        if provenance_has_clean_history:
            # Clean history already stated in provenance sentence (see
            # build_provenance_sentence()'s trailing-letter/clean_history
            # branch) — only return a sentence if there's a genuinely new
            # positive signal worth adding on its own.
            additional_signals = []
            if all_service_mb:
                additional_signals.append(
                    "all service performed at authorized Mercedes-Benz dealers"
                )
            if miles_per_year and miles_per_year < 10000:
                additional_signals.append(
                    f"averaging {miles_per_year:,} miles per year against the "
                    "national average of roughly 15,000"
                )
            if additional_signals:
                # Not .capitalize() — it lowercases everything after the
                # first letter, which would turn "Mercedes-Benz" into
                # "mercedes-benz".
                return ". ".join(_upper_first(s) for s in additional_signals) + "."
            return None  # Nothing new to add — suppress sentence three entirely

        if provenance_has_carfax:
            base = "Clean vehicle history."  # Drop "confirmed by Carfax" — already stated
            if all_service_mb:
                base = (
                    "Clean vehicle history, with all service performed at "
                    "authorized Mercedes-Benz dealers."
                )
        else:
            base = "Clean vehicle history confirmed by Carfax."
            if all_service_mb:
                base = (
                    "Clean vehicle history confirmed by Carfax, with all service "
                    "performed at authorized Mercedes-Benz dealers."
                )
    else:
        # Title brands / airbag deployment are disqualifying events handled
        # (and gated well before ad generation) elsewhere — never write an
        # accident sentence for one.
        if cf.get("has_disqualifying_event"):
            return None

        accident_count = cf.get("accident_count")
        accident_severity = cf.get("accident_severity") or "minor"
        sev = _ACCIDENT_SEVERITY_MAP.get(accident_severity, "minor")
        if isinstance(accident_count, int) and accident_count > 0:
            count_word = _ACCIDENT_COUNT_WORDS.get(accident_count, str(accident_count))
        else:
            # Count unknown (text-regex parse only knows no_accidents is
            # False) — "one" is the only defensible default without
            # fabricating a specific number Carfax didn't confirm.
            count_word = "one"
        if provenance_has_carfax:
            base = (
                f"{count_word.capitalize()} {sev} accident event reported. "
                "No title brands, no airbag deployment."
            )
        else:
            base = (
                f"{count_word.capitalize()} {sev} accident event reported on "
                "Carfax. No title brands, no airbag deployment."
            )

    if 0 < miles_per_year < 10000:
        base += (
            f" Averaging {miles_per_year:,} miles per year against the "
            "national average of roughly 15,000."
        )

    return base


_A_SERVICE_TOKEN_RE = re.compile(
    r"\ba[\s\-]*service\b|\bservice[\s\-]*a\b|\bmaintenance a\b", re.IGNORECASE
)
_B_SERVICE_TOKEN_RE = re.compile(
    r"\bb[\s\-]*service\b|\bservice[\s\-]*b\b|\bmaintenance b\b", re.IGNORECASE
)

# Mercedes-Benz mileage-interval service naming: A Service (minor) falls on
# every 10,000-mile mark that ISN'T also a 20,000-mile mark (10K/30K/50K/
# 70K/90K); B Service (major) falls on every 20,000-mile mark (20K/40K/60K/
# 80K/100K). "20K" satisfies both the 10K and 20K tests, so the 20K check
# must run first.
_MB_SERVICE_MILEAGE_RE = re.compile(r"(\d{1,3}[,]?\d{3}|\d+[kK])\s*[mM]ile")

_MB_SERVICE_FULL_LABEL = {
    "A Service (minor scheduled service)": "A Service (10,000-mile minor scheduled service)",
    "B Service (major scheduled service)": "B Service (20,000-mile major scheduled service)",
}

# A named service interval more than this far below the vehicle's current
# mileage reads as a stale milestone ("50,000-mile scheduled service" on a
# 95,000-mile car implies the car barely has 50K on it) rather than a
# meaningful confidence signal — build_recon_sentence() falls back to
# generic phrasing instead of naming the interval when the gap exceeds this.
_STALE_SERVICE_MILEAGE_GAP = 15_000


def _parse_mb_service_mileage(raw: str) -> int:
    """Turn a mileage token matched by `_MB_SERVICE_MILEAGE_RE` ("30,000",
    "30000", "30K") into its integer mile value."""
    if raw[-1] in ("k", "K"):
        return int(raw[:-1]) * 1000
    return int(raw.replace(",", ""))


def _classify_mb_service(description: str) -> str | None:
    """Classify a Mercedes-Benz scheduled-service line item as A Service
    (10,000-mile minor interval) or B Service (20,000-mile major interval).

    A description that already names "A Service"/"B Service" (or "Service
    A"/"Service B", "Maintenance A"/"Maintenance B") is classified from that
    text as-is — no mileage math needed. Otherwise the mileage named in the
    description decides: a multiple of 20,000 is B Service, a multiple of
    10,000 that isn't also a multiple of 20,000 is A Service. Returns None
    when the description names neither a service letter nor a recognizable
    mileage interval.
    """
    if _B_SERVICE_TOKEN_RE.search(description):
        return "B Service (major scheduled service)"
    if _A_SERVICE_TOKEN_RE.search(description):
        return "A Service (minor scheduled service)"

    match = _MB_SERVICE_MILEAGE_RE.search(description)
    if not match:
        return None
    mileage = _parse_mb_service_mileage(match.group(1))
    if mileage % 20_000 == 0:
        return "B Service (major scheduled service)"
    if mileage % 10_000 == 0:
        return "A Service (minor scheduled service)"
    return None


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


def _upper_first(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def build_recon_sentence(
    recon_data: dict[str, Any] | None,
    status_code: int | None,
    current_mileage: int | None = None,
) -> str | None:
    """Pre-written sentence for paragraph one sentence four, built from the
    _filter_recon() output — i.e. aggregate()'s `recon_block`, which carries
    the summary flags (scheduled_service_done, all_tires_replaced,
    brake_service_done, spark_plugs_replaced, air_filter_replaced,
    oil_change_done, wiper_blades_replaced) and the already-filtered `line_items` list.
    NOT the raw ReconVision scrape (`recon_raw`) — that dict has none of
    these summary flags, only a raw, unfiltered line-item list.

    Brake / spark-plug / air-filter / wiper-blade text is read straight off
    the matching `line_items` entry rather than rebuilt here: _filter_recon()
    already bakes the correct tier suffix (_tier_suffix()) and front/rear
    wording into each one's description, so re-deriving that logic here
    would risk drifting out of sync with it.

    Note: there is no "exactly two tires" signal anywhere in _filter_recon()'s
    output. A partial tire replacement is either dropped entirely (every tier
    except As-Is) or collapses into the single_tire_replaced flag (As-Is
    only, no axle recorded) — so only the full-set and As-Is single-tire
    cases are handled below.
    """
    if not recon_data:
        return None

    line_items = recon_data.get("line_items") or []
    by_reason: dict[str, list[dict[str, Any]]] = {}
    for li in line_items:
        by_reason.setdefault(li.get("recon_reason"), []).append(li)

    suffix = _tier_suffix(status_code if status_code is not None else 10)
    components: list[str] = []

    # 1. SCHEDULED SERVICE — Mercedes-Benz A/B Service naming only applies at
    # the MB CPO tiers (10/16); Hendrick Certified/Affordable/As-Is (11/12/13)
    # cover every make, so those get brand-neutral, mileage-only phrasing
    # instead of a Mercedes-Benz-specific A/B classification.
    if recon_data.get("scheduled_service_done"):
        service_items = by_reason.get("scheduled_service") or []
        service_component = None

        if status_code in (10, 16):
            # No staleness check here: A/B Service naming always describes the
            # service CLASS's fixed interval (10K minor / 20K major) — "B
            # Service (20,000-mile major scheduled service)" is equally
            # accurate whether it's the 1st or 5th B Service, never a specific
            # odometer milestone the car "just now" reached. Only the
            # brand-neutral branch below quotes a literal large mileage
            # number and can go stale.
            for li in service_items:
                classified = _classify_mb_service(li.get("description") or "")
                if classified:
                    service_component = (
                        f"Mercedes-Benz {_MB_SERVICE_FULL_LABEL[classified]} "
                        f"completed {suffix}"
                    )
                    break
            if service_component is None:
                service_component = f"scheduled Mercedes-Benz service completed {suffix}"
        else:
            for li in service_items:
                match = _MB_SERVICE_MILEAGE_RE.search(li.get("description") or "")
                if not match:
                    continue
                mileage = _parse_mb_service_mileage(match.group(1))
                stale = (
                    current_mileage is not None
                    and mileage < current_mileage - _STALE_SERVICE_MILEAGE_GAP
                )
                if stale:
                    continue
                service_component = (
                    f"{mileage:,}-mile scheduled service completed {suffix}"
                )
                break
            if service_component is None:
                # Brand-neutral fallback — this tier also covers non-MB makes
                # (see the comment above), so a stale interval falls back to
                # the same generic phrasing already used when no mileage can
                # be classified at all, rather than a Mercedes-Benz-specific
                # phrase that would misdescribe a non-MB vehicle.
                service_component = f"scheduled service completed {suffix}"

        components.append(service_component)

    # 2. TIRES
    if recon_data.get("all_tires_replaced"):
        components.append(f"four new manufacturer-recommended tires installed {suffix}")
    elif recon_data.get("single_tire_replaced"):
        components.append(f"one new manufacturer-recommended tire installed {suffix}")

    # 3. BRAKES — front/rear wording and tier suffix already baked in.
    if recon_data.get("brake_service_done"):
        items = by_reason.get("brake_service") or []
        desc = items[0].get("description") if items else None
        components.append(_lower_first(desc) if desc else f"brakes replaced {suffix}")

    # 4. SPARK PLUGS
    if recon_data.get("spark_plugs_replaced"):
        items = by_reason.get("spark_plugs") or []
        desc = items[0].get("description") if items else None
        components.append(_lower_first(desc) if desc else f"spark plugs replaced {suffix}")

    # 5. AIR FILTER — only alongside another item. _filter_recon() already
    # enforces this on its own; the `components` check here is redundant
    # belt-and-suspenders, not the primary gate.
    if recon_data.get("air_filter_replaced") and components:
        items = by_reason.get("air_filter") or []
        desc = items[0].get("description") if items else None
        components.append(_lower_first(desc) if desc else f"air filter replaced {suffix}")

    # 6. WIPER BLADES — same rule as air filter.
    if recon_data.get("wiper_blades_replaced") and components:
        items = by_reason.get("wiper_blades") or []
        desc = items[0].get("description") if items else None
        components.append(_lower_first(desc) if desc else f"wiper blades replaced {suffix}")

    # 7. OIL CHANGE — same rule as air filter and wiper blades.
    if recon_data.get("oil_change_done") and components:
        items = by_reason.get("oil_change") or []
        desc = items[0].get("description") if items else None
        components.append(_lower_first(desc) if desc else f"oil and filter changed {suffix}")

    if not components:
        return None
    # Each component may carry the tier suffix already (either appended above
    # or baked in upstream by _filter_recon()) — strip it from every
    # component so it appears exactly once, at the end of the full sentence,
    # regardless of how many recon items are present.
    stripped = []
    for c in components:
        c = c.strip()
        if c.endswith(suffix):
            c = c[: -len(suffix)].rstrip()
        stripped.append(c)
    if len(stripped) == 1:
        return f"{_upper_first(stripped[0])} {suffix}."
    if len(stripped) == 2:
        return f"{_upper_first(stripped[0])} and {stripped[1]}, all {suffix}."
    # Oxford comma for 3+.
    return _upper_first(", ".join(stripped[:-1])) + f", and {stripped[-1]}, all {suffix}."


_PROOF_POINT_FAVORABLE_THRESHOLD = 1000


def _proof_point_benchmark(proof_points: list[dict[str, Any]], key: str) -> float | None:
    for p in proof_points or []:
        if p.get("key") == key and p.get("benchmark_price") is not None:
            return p.get("benchmark_price")
    return None


def _model_from_ymm(year_make_model: str | None) -> str | None:
    """'2024 Mercedes-Benz GLE 350' -> 'GLE 350'. None if year_make_model is
    missing or doesn't start with a 4-digit model year (never guess)."""
    if not year_make_model:
        return None
    m = re.match(r"\s*(?:19|20)\d{2}\s+\S+\s*(.*)$", year_make_model)
    if not m:
        return None
    return m.group(1).strip() or None


def _get_market_scope(search_distance: Any) -> str:
    """Geographic framing for the proof-point sentences' comparable-set
    language, in place of the raw search-radius number ACV Max's Define
    Competitive Set filter uses (see scraper._SEARCH_DISTANCE_RE /
    pricing_raw["search_distance"]). Falls back to the widest-audience-safe
    phrase whenever the radius is missing or unparseable — never guess a
    narrower region than the data supports."""
    if search_distance is None:
        return "across the regional market"
    try:
        miles = int(search_distance)
    except (TypeError, ValueError):
        return "across the regional market"
    if miles <= 50:
        return "across the Triangle"
    elif miles <= 150:
        return "across central North Carolina"
    elif miles <= 250:
        return "across the Carolinas"
    elif miles <= 500:
        return "across the Southeast"
    elif miles <= 1000:
        return "across the Eastern Seaboard"
    else:
        return "nationwide"


# Matching-configuration days supply must turn at least this much faster than
# the overall market to count as a demand signal on its own — see the
# VELOCITY ANCHOR FALLBACK in build_proof_point_sentence().
_VELOCITY_ANCHOR_DAYS_SUPPLY_RATIO = 0.75
_VELOCITY_ANCHOR_SCARCITY_THRESHOLD = 15
# CASE B of the velocity anchor fallback: current price above every
# benchmark, but not by more than this fraction of market average, still
# reads as "in the game" rather than overpriced.
_VELOCITY_ANCHOR_MAX_MARKET_PCT = 0.03


def build_proof_point_sentence(
    pricing_data: dict[str, Any] | None,
    advertised_price: float | None,
    current_price: float | None = None,
    admin_fee: int = DEALER_DOC_FEE,
    search_distance: Any = None,
) -> tuple[str | None, str]:
    """Pre-written paragraph-two pricing sentence, plus its proof_point_type
    ("standard" or "velocity_anchor" — see below) so format_data_package()
    can label which kind of proof point the model received. Primary anchor
    is whichever of J.D. Power / Kelley Blue Book Typical Listing Price has
    the larger favorable gap (more than $1,000 below current_price); secondary
    is active regional market listings when independently favorable by the
    same margin.

    Gaps are calculated against `current_price` — the raw, pre-fee ACV Max
    internet price — never `advertised_price`. ACV Max's own benchmark prices
    are already computed against that raw price, so comparing them against
    advertised_price (current_price + admin_fee) would double-count the fee
    and understate every favorable gap by admin_fee. advertised_price is used
    only for the price shown to the buyer in the sentence text. When the
    caller doesn't have current_price handy, it's derived as
    advertised_price - admin_fee (exact, since that's how advertised_price
    itself is built — see _advertised_price()).

    Every gap below is `benchmark - current_price`: positive means current
    price is that much BELOW the benchmark (favorable to the buyer),
    negative means current price is that much ABOVE it.

    `pricing_data` is the raw ACV Max pricing dict (pricing_raw) — its
    benchmark prices live in the `pricing_proof_points` list (one dict per
    benchmark, keyed by "jd_power" / "typical_listing_price" /
    "market_average", with a "benchmark_price" field), not as flat top-level
    fields; matching_market_days / overall_market_days / matching_count
    (used by the VELOCITY ANCHOR FALLBACK below) ARE flat top-level fields
    on pricing_raw instead (see _market_velocity()).

    NOTE: this max-gap comparison between J.D. Power and KBB, and the $1,000
    favorability floor, are a different selection rule from the pipeline's
    existing proof-point picker (_select_proof_points(), already exposed as
    pkg["pricing"]["primary_proof_point"]/["secondary_proof_point"]) — that
    one always prefers KBB whenever it's favorable at all, regardless of gap
    size, and has no dollar floor. The two will disagree whenever KBB is
    favorable but by less than J.D. Power's gap, or by $1,000 or less.

    VELOCITY ANCHOR FALLBACK: when neither book value nor the market average
    clears the $1,000 favorability floor, this configuration turning over
    meaningfully faster than the overall market (matching_market_days under
    _VELOCITY_ANCHOR_DAYS_SUPPLY_RATIO of overall_market_days) is still a
    real, defensible demand signal — but the wording must match what's
    actually true about price position, never a blanket "below every
    benchmark" claim. Three cases, checked in order:

      CASE A — current price is below at least one book benchmark (KBB or
      J.D. Power), by any amount, even under the $1,000 floor above. Uses
      the generic "at or below Kelley Blue Book and J.D. Power benchmarks"
      position clause plus the velocity/demand sentence.

      CASE B — current price is at or above both book benchmarks, but within
      _VELOCITY_ANCHOR_MAX_MARKET_PCT (3%) of market average. No price-
      position claim at all (there isn't a favorable one to make) — just the
      velocity/demand sentence on its own.

      CASE C — current price is above every benchmark by more than 3% of
      market average (or market average itself isn't available to check).
      The velocity anchor never fires here, regardless of demand — a price
      that's meaningfully above the market doesn't get to lean on a "turns
      fast" story. Falls through to the bare competitively-positioned
      sentence.

    Returns (None, "standard") when advertised_price is missing; every other
    case returns an actual sentence — a bare price with no supporting
    context is never returned (see the final fallback).

    search_distance is ACV Max's Define Competitive Set search radius, in
    miles (pricing_raw["search_distance"] — see scraper._SEARCH_DISTANCE_RE).
    Every reference to the comparable-set's geography in the sentences below
    goes through _get_market_scope(search_distance) instead of a bare
    "regional market"/"nationwide" guess, so the wording tracks whatever
    radius ACV Max actually searched. None falls back to the generic
    "across the regional market" phrase.
    """
    if advertised_price is None:
        return None, "standard"
    if current_price is None:
        current_price = advertised_price - admin_fee

    market_scope = _get_market_scope(search_distance)

    pd = pricing_data if isinstance(pricing_data, dict) else {}
    proof_points = pd.get("pricing_proof_points") or []

    jdpower_price = _proof_point_benchmark(proof_points, _JD_POWER_PROOF_POINT_KEY)
    kbb_price = _proof_point_benchmark(proof_points, _KBB_PROOF_POINT_KEY)
    market_price = _proof_point_benchmark(proof_points, _MARKET_PROOF_POINT_KEY)

    jdpower_gap = jdpower_price - current_price if jdpower_price is not None else None
    kbb_gap = kbb_price - current_price if kbb_price is not None else None
    market_gap = market_price - current_price if market_price is not None else None

    jdpower_favorable = jdpower_gap is not None and jdpower_gap > _PROOF_POINT_FAVORABLE_THRESHOLD
    kbb_favorable = kbb_gap is not None and kbb_gap > _PROOF_POINT_FAVORABLE_THRESHOLD
    market_favorable = market_gap is not None and market_gap > _PROOF_POINT_FAVORABLE_THRESHOLD

    book_options = []
    if jdpower_favorable:
        book_options.append((_BOOK_VALUE_LABELS[_JD_POWER_PROOF_POINT_KEY], jdpower_gap))
    if kbb_favorable:
        book_options.append((_BOOK_VALUE_LABELS[_KBB_PROOF_POINT_KEY], kbb_gap))

    if book_options:
        primary_label, primary_gap = max(book_options, key=lambda x: x[1])
    else:
        primary_label, primary_gap = None, 0

    if market_favorable:
        secondary_gap = market_gap
        secondary_label = f"active listings {market_scope}"
    else:
        secondary_gap = 0
        secondary_label = None

    price_str = f"${advertised_price:,.0f}"
    fee_str = f"${admin_fee:,}"
    base = f"Current asking price is {price_str} (includes {fee_str} dealer administrative fee)"

    if primary_label and secondary_label:
        return (
            f"{base}, ${primary_gap:,.0f} below {primary_label} and "
            f"${secondary_gap:,.0f} below {secondary_label} for comparable units.",
            "standard",
        )
    if primary_label:
        return f"{base}, ${primary_gap:,.0f} below {primary_label} for comparable units.", "standard"
    if secondary_label:
        return f"{base}, ${secondary_gap:,.0f} below {secondary_label} for comparable units.", "standard"

    # Neither book value nor market cleared the $1,000 floor. Try the
    # velocity/scarcity anchor (CASE A/B/C above) before falling all the way
    # back to a bare price with no context.
    matching_days_supply = pd.get("matching_market_days")
    overall_days_supply = pd.get("overall_market_days")
    matching_count = pd.get("matching_count")

    velocity_days_ok = (
        matching_days_supply is not None
        and overall_days_supply is not None
        and matching_days_supply < overall_days_supply * _VELOCITY_ANCHOR_DAYS_SUPPLY_RATIO
    )

    has_book_edge = (jdpower_gap is not None and jdpower_gap > 0) or (
        kbb_gap is not None and kbb_gap > 0
    )

    percent_over_market = None
    if market_price:
        percent_over_market = (current_price - market_price) / market_price

    def _with_scarcity(sentence: str) -> str:
        # A thin comp count only means something as a rarity claim when the
        # search actually went nationwide — a thin count from a 500-mile
        # regional search just means the search radius should be widened
        # when pricing this vehicle, not a selling point for the ad.
        if (
            matching_count is not None
            and matching_count < _VELOCITY_ANCHOR_SCARCITY_THRESHOLD
            and market_scope == "nationwide"
        ):
            return sentence + (
                f" Fewer than {matching_count} comparable examples are actively listed nationwide."
            )
        return sentence

    velocity_model = _model_from_ymm(pd.get("year_make_model"))

    if velocity_days_ok and overall_days_supply is not None and velocity_model:
        velocity_sentence = (
            f"While broader {velocity_model} inventory averages {overall_days_supply:,.0f} "
            f"days on the market, comparable units with this configuration turn in just "
            f"{matching_days_supply:,.0f} days {market_scope}, reflecting strong demand for "
            f"this specific specification."
        )
    elif velocity_days_ok:
        velocity_sentence = (
            f"Comparable units with this configuration turn in an average of "
            f"{matching_days_supply:,.0f} days {market_scope}, reflecting strong demand for "
            f"this specific specification."
        )
    else:
        velocity_sentence = None

    if velocity_days_ok and has_book_edge:
        # CASE A — below at least one book benchmark, any amount. Only name
        # the benchmark(s) that were actually available AND favorable — a
        # benchmark with no price at all (jdpower_gap/kbb_gap is None) must
        # never be claimed as "at or below" alongside one that genuinely was.
        edge_labels = []
        if jdpower_gap is not None and jdpower_gap > 0:
            edge_labels.append(_BOOK_VALUE_LABELS[_JD_POWER_PROOF_POINT_KEY])
        if kbb_gap is not None and kbb_gap > 0:
            edge_labels.append(_BOOK_VALUE_LABELS[_KBB_PROOF_POINT_KEY])
        edge_str = " and ".join(edge_labels) if edge_labels else "book value"
        sentence = (
            f"{base}, at or below {edge_str} for "
            f"comparable units. {velocity_sentence}"
        )
        return _with_scarcity(sentence), "velocity_anchor"

    if (
        velocity_days_ok
        and not has_book_edge
        and percent_over_market is not None
        and percent_over_market <= _VELOCITY_ANCHOR_MAX_MARKET_PCT
    ):
        # CASE B — above every book benchmark, but within 3% of market average.
        sentence = f"{base}. {velocity_sentence}"
        return _with_scarcity(sentence), "velocity_anchor"

    # CASE C — meaningfully above every benchmark (or the velocity/demand
    # signal itself isn't available) — no velocity anchor, bare fallback.
    return f"{base}, competitively positioned against active listings {market_scope}.", "standard"


# Makes where MSRP is routinely padded by $10k+ of options — a $5,000 gap
# common on mainstream makes doesn't clear the "meaningful depreciation" bar
# for these, so the dollar floor is proportionally higher (see
# build_msrp_sentence()'s decision tree).
LUXURY_MAKES = {
    "mercedes", "mercedes-benz", "bmw", "porsche", "audi", "lexus",
    "jaguar", "land rover", "bentley", "rolls-royce", "maserati",
    "ferrari", "lamborghini", "genesis", "cadillac", "lincoln", "acura",
    "infiniti", "volvo", "alfa",
}

# Above this fraction of MSRP shaved off, a raw dollar depreciation figure
# reads as a lemon signal rather than a deal — build_msrp_sentence() switches
# to the equipment-anchor framing (factory window sticker, no dollar gap)
# instead once the gap clears it.
_MSRP_MAX_PCT_DROP = 0.42


def build_msrp_sentence(
    total_msrp: float | None,
    advertised_price: float | None,
    status_code: int | None,
    year_make_model: str | None,
) -> str | None:
    """Pre-written MSRP sentence for paragraph two, or None when MSRP
    shouldn't be discussed at all.

    Guards against a missing total_msrp/advertised_price BEFORE computing the
    gap — total_msrp is routinely None (MSRP unavailable is a normal,
    expected case; see MSRP UNAVAILABLE RULE in SYSTEM_PROMPT).

    AGE GATE — MSRP depreciation is only a credible story on a vehicle 3
    model years old or newer. year_make_model's leading token must parse as
    the model year, or nothing is returned (never guess a make/year off
    malformed input).

    THRESHOLDS — the minimum dollar gap worth mentioning is higher for
    LUXURY_MAKES ($15,000) than everyone else ($5,000), since luxury MSRPs
    are routinely padded by that much in options alone. Once the gap clears
    that floor, a percent-of-MSRP drop above _MSRP_MAX_PCT_DROP (42%) reads
    as a lemon signal rather than a deal, so the sentence switches to an
    equipment-anchor framing (the factory window sticker figure, no dollar
    gap) instead of the straight depreciation story.

    status_code only matters for courtesy vehicles (16), which skip the dollar
    floor entirely; every other status uses the luxury-make/age-gate/
    percent-drop tree above.
    """
    if not total_msrp or not advertised_price:
        return None

    parts = str(year_make_model).strip().split()
    try:
        model_year = int(parts[0])
    except (ValueError, IndexError):
        return None

    make = parts[1].lower() if len(parts) > 1 else ""
    is_luxury = make in LUXURY_MAKES

    vehicle_age = date.today().year - model_year
    if vehicle_age > 3:
        # Year 4+ — omit MSRP entirely.
        print(
            f"[aggregator] MSRP check: model_year={model_year}, age={vehicle_age} "
            f"— age-gated (>3 years), no MSRP sentence",
            file=sys.stderr,
        )
        return None

    msrp_gap = total_msrp - advertised_price
    if msrp_gap <= 0:
        return None

    pct_drop = msrp_gap / total_msrp
    # Courtesy/loaner vehicles (status 16) always get an MSRP comparison
    # regardless of the usual luxury/non-luxury dollar floor — the market
    # comp story is frequently weak or unavailable on these (unusual
    # pricing distance from a normal CPO comp pool, thin comp sets), so the
    # MSRP anchor is often the strongest or only compelling number
    # available. The age gate above still applies, but these vehicles are
    # always current-model-year so it never actually blocks them.
    min_dollar_threshold = 0 if status_code == 16 else (15000 if is_luxury else 5000)
    print(
        f"[aggregator] MSRP check: model_year={model_year}, age={vehicle_age}, "
        f"gap=${msrp_gap:,.0f}, pct={pct_drop:.1%}, threshold=${min_dollar_threshold:,}",
        file=sys.stderr,
    )

    if msrp_gap >= min_dollar_threshold and pct_drop <= _MSRP_MAX_PCT_DROP:
        return (
            f"Original MSRP was ${total_msrp:,.0f}. At ${advertised_price:,.0f}, "
            f"this represents ${msrp_gap:,.0f} in depreciation the next buyer does not absorb."
        )
    if msrp_gap >= min_dollar_threshold and pct_drop > _MSRP_MAX_PCT_DROP:
        # High depreciation — equipment anchor instead of the dollar gap.
        return (
            f"Originally ordered with a ${total_msrp:,.0f} factory window sticker, "
            f"reflecting significant factory options above base trim levels."
        )
    return None


# --------------------------------------------------------------------------- #
# Peacock flag + calculated factory-warranty remaining
# --------------------------------------------------------------------------- #

MB_WARRANTY_MONTHS = 48
MB_WARRANTY_MILES = 50_000
MB_BATTERY_WARRANTY_MONTHS = 120
MB_BATTERY_WARRANTY_MILES = 100_000

# Basic/bumper-to-bumper factory warranty periods (years, miles) by make, for
# build_warranty_sentence()'s As-Is (status 13) branch — As-Is inventory spans
# arbitrary non-MB makes, unlike MB_WARRANTY_MONTHS/MILES above. Basic warranty
# only, never powertrain (powertrain periods run longer and would overstate
# what's actually still covered).
WARRANTY_PERIODS: dict[str, tuple[int, int]] = {
    "hyundai": (5, 60000),
    "kia": (5, 60000),
    "genesis": (5, 60000),
    "toyota": (3, 36000),
    "lexus": (4, 48000),
    "honda": (3, 36000),
    "acura": (4, 50000),
    "bmw": (4, 50000),
    "mini": (4, 50000),
    "volkswagen": (4, 50000),
    "audi": (4, 50000),
    "porsche": (4, 50000),
    "volvo": (4, 50000),
    "jaguar": (5, 60000),
    "land rover": (5, 60000),
    "alfa romeo": (4, 50000),
    "jeep": (3, 36000),
    "ford": (3, 36000),
    "chevrolet": (3, 36000),
    "gmc": (3, 36000),
    "cadillac": (4, 50000),
    "buick": (4, 50000),
    "nissan": (3, 36000),
    "infiniti": (4, 50000),
    "mazda": (3, 36000),
    "subaru": (3, 36000),
    "default": (3, 36000),
}


def _warranty_period_for_make(
    year_make_model: str | None,
) -> tuple[str, tuple[int, int]] | None:
    """(make display name, (years, miles)) from WARRANTY_PERIODS, or None if
    year_make_model doesn't even have a make token to work with.

    Checks the two-word make ("Land Rover", "Alfa Romeo") before falling back
    to the single word immediately after the model year — WARRANTY_PERIODS
    has two two-word keys, and extracting only the literal second word would
    never match them ("Land" alone isn't in the table).
    """
    words = (year_make_model or "").split()
    if len(words) < 2:
        return None
    if len(words) >= 3:
        two_word = f"{words[1]} {words[2]}"
        if two_word.lower() in WARRANTY_PERIODS:
            return two_word, WARRANTY_PERIODS[two_word.lower()]
    make = words[1]
    return make, WARRANTY_PERIODS.get(make.lower(), WARRANTY_PERIODS["default"])

# Mercedes EQ model tokens: EQS EQE EQB EQA EQC EQV.
_EV_MODEL_RE = re.compile(r"\bEQ[SEBACV]\b", re.IGNORECASE)

# Carfax "vehicle first sold / in-service" date patterns, most specific first.
_CARFAX_SALE_DATE_RES = [
    re.compile(r"(\d{1,2}/\d{1,2}/\d{4})[^\n]{0,40}?vehicle\s+(?:first\s+)?(?:sold|purchased)", re.IGNORECASE),
    re.compile(r"vehicle\s+(?:first\s+)?(?:sold|purchased)[^\n]{0,40}?(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE),
    re.compile(r"(?:in[-\s]?service|warranty\s+start|first\s+owner)[^\n]{0,40}?(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE),
    re.compile(r"\bNew\b[^\n]{0,20}?(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE),
]


def _ymm_year_model(year_make_model: str | None) -> tuple[int | None, str | None]:
    """'2023 Mercedes-Benz EQE 500' -> (2023, 'EQE 500')."""
    if not year_make_model:
        return None, None
    m = re.match(r"\s*((?:19|20)\d{2})\s+\S+\s+(.*)$", year_make_model)
    if not m:
        return None, None
    return int(m.group(1)), (m.group(2).strip() or None)


def _months_between(d0: date, d1: date) -> int:
    months = (d1.year - d0.year) * 12 + (d1.month - d0.month)
    if d1.day < d0.day:
        months -= 1
    return max(0, months)


def _carfax_sale_date(raw_text: str | None) -> date | None:
    if not raw_text:
        return None
    for rx in _CARFAX_SALE_DATE_RES:
        m = rx.search(raw_text)
        if m:
            try:
                return datetime.strptime(m.group(1), "%m/%d/%Y").date()
            except ValueError:
                continue
    # last resort: the earliest plausible MM/DD/YYYY anywhere in the report
    seen: list[date] = []
    today = date.today()
    for m in re.finditer(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", raw_text):
        try:
            d = datetime.strptime(m.group(1), "%m/%d/%Y").date()
        except ValueError:
            continue
        if 2000 <= d.year <= today.year:
            seen.append(d)
    return min(seen) if seen else None


def _peacock_fields(
    msrp_data: dict[str, Any] | None, status_code: int | None
) -> dict[str, Any]:
    """Count option packages priced >= $1,000. peacock_mode is on when fewer
    than two such packages are present (a lightly equipped car) — but only
    for MB CPO inventory (status 10/16, see MB_CPO_GATE_STATUS_CODES). Non-MB
    programs (11/12/13, ...) price options differently and put much of their
    equipment at the trim level rather than as priced packages, so a low
    high-value-option count there isn't a signal of a lightly equipped car;
    peacock_mode is always False off the MB CPO tier."""
    opts = (msrp_data or {}).get("option_packages") or []
    count = sum(
        1
        for o in opts
        if isinstance(o.get("price"), (int, float)) and o.get("price") >= 1000
    )
    peacock_mode = count < 2 and status_code in MB_CPO_GATE_STATUS_CODES
    return {"high_value_option_count": count, "peacock_mode": peacock_mode}


def _warranty_fields(
    pricing_raw: dict[str, Any], carfax_raw: dict[str, Any] | None
) -> dict[str, Any]:
    """Best-effort remaining factory warranty from the Carfax first-sold date and
    the ACV MAX odometer. MB standard is 48 mo / 50,000 mi; EQ models also get a
    120 mo / 100,000 mi battery warranty. Only marks warranty_calculated True
    when both remainders are positive and Carfax says the warranty is claimable.
    """
    cf = carfax_raw if isinstance(carfax_raw, dict) else {}
    ymm = pricing_raw.get("year_make_model")
    year, model = _ymm_year_model(ymm)
    current_mileage = pricing_raw.get("mileage")
    is_ev = bool(model and _EV_MODEL_RE.search(model))
    warranty_claimable = bool(cf.get("warranty_claimable"))

    sale_date = _carfax_sale_date(cf.get("raw_text"))
    if sale_date is None and year:
        # conservative fallback: assume in service Jan 1 of the model year
        # (over-states months elapsed -> under-states remaining -> safe)
        sale_date = date(year, 1, 1)
    months_elapsed = _months_between(sale_date, date.today()) if sale_date else None

    out: dict[str, Any] = {
        "warranty_claimable": warranty_claimable,
        "warranty_start_date": sale_date.isoformat() if sale_date else None,
        "warranty_remaining_months": None,
        "warranty_remaining_miles": None,
        "warranty_calculated": False,
        "battery_warranty_remaining_months": None,
        "battery_warranty_remaining_miles": None,
    }

    if months_elapsed is not None:
        out["warranty_remaining_months"] = max(0, MB_WARRANTY_MONTHS - months_elapsed)
    if isinstance(current_mileage, (int, float)):
        out["warranty_remaining_miles"] = max(
            0, MB_WARRANTY_MILES - int(current_mileage)
        )

    rm, ri = out["warranty_remaining_months"], out["warranty_remaining_miles"]
    out["warranty_calculated"] = bool(
        warranty_claimable and rm and ri and rm > 0 and ri > 0
    )

    if is_ev:
        if months_elapsed is not None:
            out["battery_warranty_remaining_months"] = max(
                0, MB_BATTERY_WARRANTY_MONTHS - months_elapsed
            )
        if isinstance(current_mileage, (int, float)):
            out["battery_warranty_remaining_miles"] = max(
                0, MB_BATTERY_WARRANTY_MILES - int(current_mileage)
            )

    return out


def build_warranty_sentence(
    carfax_data: dict[str, Any] | None,
    pricing_data: dict[str, Any] | None,
    status_code: int | None,
    year_make_model: str | None,
) -> str | None:
    """Pre-written warranty-remaining sentence for paragraph two, or None when
    no warranty language applies for this tier or vehicle.

    NOTE (status 10 MB CPO): warranty_remaining_months/miles are NOT fields on
    carfax_data — _warranty_fields() computes them fresh from Carfax's
    in-service date and the current odometer and returns them as their own
    dict, never written back onto carfax_raw. This calls _warranty_fields()
    directly rather than reading nonexistent keys off carfax_data.

    NOTE (status 13 As-Is): basic/bumper-to-bumper warranty period by make,
    from WARRANTY_PERIODS (verified periods, not powertrain — powertrain
    periods run longer and would overstate what's actually still covered).
    See _warranty_period_for_make() for the make-extraction rule.
    """
    cf = carfax_data if isinstance(carfax_data, dict) else {}
    pr = pricing_data if isinstance(pricing_data, dict) else {}

    if status_code == 16:
        return None  # warranty language already lives in the provenance sentence

    if status_code == 10:
        wf = _warranty_fields(pr, cf)
        if not wf.get("warranty_claimable"):
            return None
        months = wf.get("warranty_remaining_months")
        miles = wf.get("warranty_remaining_miles")
        if not months or not miles:
            return None
        return (
            f"This vehicle carries an estimated {months} months and {miles:,} "
            "miles of remaining factory warranty coverage, with an additional "
            "year of unlimited-mile Certified Pre-Owned coverage beyond that."
        )

    if status_code == 11:
        model_year, _ = _ymm_year_model(year_make_model)
        if not model_year:
            return None
        current_mileage = pr.get("mileage")
        if current_mileage is None:
            current_mileage = cf.get("last_reported_odometer")
        if not isinstance(current_mileage, (int, float)):
            return None
        miles_remaining = 100_000 - int(current_mileage)
        if miles_remaining <= 0:
            return None
        expiry_year = model_year + 10
        return (
            f"The powertrain warranty runs through January 1, {expiry_year} "
            f"with {miles_remaining:,} miles of odometer room remaining. "
            "Whichever limit comes first ends coverage. The High-Tech "
            "Warranty adds 12 months or 12,000 miles of coverage from the "
            "date of purchase."
        )

    if status_code == 12:
        return (
            "This vehicle carries a 3-month/3,000-mile Hendrick Affordable "
            "Limited Powertrain Warranty beginning on the date of purchase."
        )

    if status_code == 13:
        period = _warranty_period_for_make(year_make_model)
        if period is None:
            return None
        make, (years, warranty_miles) = period

        model_year, _ = _ymm_year_model(year_make_model)
        if not model_year:
            return None
        months_remaining = years * 12 - _months_between(date(model_year, 1, 1), date.today())
        if months_remaining <= 0:
            return None

        current_mileage = pr.get("mileage")
        if current_mileage is None:
            current_mileage = cf.get("last_reported_odometer")
        if not isinstance(current_mileage, (int, float)):
            return None
        miles_remaining = warranty_miles - int(current_mileage)
        if miles_remaining <= 0:
            return None

        return (
            f"This vehicle carries {months_remaining} months and "
            f"{miles_remaining:,} miles of remaining {make} factory warranty "
            "transferable to the new owner."
        )

    # Any unmapped status code: no warranty language.
    return None


SHIPPING_SENTENCE = (
    "Nationwide transport and door-to-door carrier delivery are available "
    "for out-of-state buyers. Our team regularly coordinates seamless "
    "long-distance purchases across the country."
)

_ENTHUSIAST_MAKES = {
    "alfa", "romeo", "porsche", "ferrari", "maserati",
    "lamborghini", "bentley", "rolls-royce", "rolls",
}


def _shipping_trigger_flags(
    pricing_raw: dict[str, Any],
    advertised_price: float | None,
    year_make_model: str | None,
) -> bool:
    """Shared status 11/13 trigger check: enthusiast make, manual
    transmission, tight regional scarcity, price above $60k, or an
    aggressively-priced-vs-market gap."""
    words = (year_make_model or "").split()
    make = words[1].lower() if len(words) > 1 else ""
    is_enthusiast = make in _ENTHUSIAST_MAKES
    has_manual = "manual" in str(pricing_raw.get("transmission", "")).lower()
    matching_count = pricing_raw.get("matching_count", 999)
    price_above_60k = advertised_price is not None and advertised_price > 60000

    proof_points = pricing_raw.get("pricing_proof_points") or []
    market_price = _proof_point_benchmark(proof_points, _MARKET_PROOF_POINT_KEY)
    market_gap = (
        advertised_price - market_price
        if market_price is not None and advertised_price is not None
        else 0
    )
    aggressively_priced = market_gap < -3000

    return (
        is_enthusiast
        or has_manual
        or matching_count < 10
        or price_above_60k
        or aggressively_priced
    )


_MANUFAKTUR_INTERIOR_KEYWORDS = [
    "headliner", "leather", "nappa", "mb-tex", "trim", "floor", "carpet",
]


def _is_manufaktur_exterior(option_packages: list[dict[str, Any]]) -> bool:
    """True when a MANUFAKTUR option package name doesn't look like an
    interior option — see MANUFAKTUR TRIGGER SCOPE in SYSTEM_PROMPT (the
    trigger is exterior paint only, never interior trim like headliners or
    leather)."""
    for pkg in option_packages:
        name = str(pkg.get("name", "")).upper()
        if "MANUFAKTUR" in name:
            name_lower = name.lower()
            if not any(kw in name_lower for kw in _MANUFAKTUR_INTERIOR_KEYWORDS):
                return True
    return False


def build_shipping_sentence(
    pricing_raw: dict[str, Any] | None,
    msrp_data: dict[str, Any] | None,
    status_code: int | None,
    year_make_model: str | None,
    warranty_sentence: str | None = None,
) -> str | None:
    """Pre-written nationwide-shipping sentence for the end of paragraph two,
    or None when nothing triggers it.

    NOTE: `pricing_raw` is the raw ACV Max dict. matching_count is a flat
    field on it (see _market_velocity()), not nested under a "market_velocity"
    key — pricing_raw has no such nested key at all, only the final pkg does.
    Likewise there's no flat "market_avg_price" field; the market benchmark
    lives in pricing_raw["pricing_proof_points"] (key "market_average"), read
    here via _proof_point_benchmark() — the same helper build_proof_point_
    sentence() uses.

    NOTE: there is no "transmission" field anywhere in the scraped pricing
    data today, so has_manual is always False in practice. This still reads
    pricing_raw.get("transmission") so the trigger activates on its own if
    that field is ever added upstream, rather than needing a rewrite here.

    NOTE: MANUFAKTUR detection uses _is_manufaktur_exterior(), which excludes
    package names carrying an interior-sounding keyword (headliner, leather,
    nappa, mb-tex, trim, floor, carpet) — see MANUFAKTUR TRIGGER SCOPE in
    SYSTEM_PROMPT (exterior paint only, never interior trim). This is a name-
    keyword heuristic, not a real exterior/interior category field (none
    exists in the option-package data), so an unusually named exterior
    package containing one of those words would be misclassified.
    """
    pr = pricing_raw if isinstance(pricing_raw, dict) else {}
    msrp = msrp_data if isinstance(msrp_data, dict) else {}

    mileage = pr.get("mileage", 999999)

    if status_code == 12:
        return None
    if status_code == 13 and mileage > 100000:
        return None

    advertised_price = _advertised_price(pr)

    if status_code in (10, 16):
        is_amg = "AMG" in str(year_make_model).upper()
        is_manufaktur = _is_manufaktur_exterior(msrp.get("option_packages", []))
        price_above_80k = advertised_price is not None and advertised_price > 80000
        matching_count = pr.get("matching_count", 999)
        scarcity = matching_count < 15

        if is_amg or is_manufaktur or price_above_80k or scarcity:
            return SHIPPING_SENTENCE
        return None

    if status_code == 11:
        if _shipping_trigger_flags(pr, advertised_price, year_make_model):
            return SHIPPING_SENTENCE
        return None

    if status_code == 13:
        warranty_active = warranty_sentence is not None
        under_60k = mileage < 60000
        between_60k_100k = 60000 <= mileage <= 100000

        if between_60k_100k and not warranty_active:
            return None

        if under_60k or warranty_active:
            if _shipping_trigger_flags(pr, advertised_price, year_make_model):
                return SHIPPING_SENTENCE
        return None

    return None


_AMG_LINE_SUV_MODELS = [
    "GLA", "GLB", "GLC", "GLE", "GLS", "EQA", "EQB", "EQC",
    "EQE SUV", "EQS SUV",
]
_AMG_LINE_SEDAN_COUPE_MODELS = [
    "C-CLASS", "C 300", "C 43", "C 63", "E-CLASS", "E 350",
    "E 450", "E 53", "S-CLASS", "S 580", "CLA", "CLS",
    "AMG GT 4", "AMG GT 53", "AMG GT 63",
]
_AMG_LINE_G_CLASS_IDENTIFIERS = [
    "G-CLASS", "G CLASS", "G 550", "G 63", "G550", "G63",
]

_AMG_LINE_SUV_DESCRIPTION = (
    "body color painted fender flares replacing standard black plastic wheel arches"
)
_AMG_LINE_SEDAN_COUPE_DESCRIPTION = (
    "AMG-specific front apron, sculpted side sills, and AMG rear apron with diffuser"
)
_AMG_LINE_G_CLASS_DESCRIPTION = "AMG-specific exterior styling elements"
_AMG_LINE_UNKNOWN_DESCRIPTION = "AMG exterior styling elements"


def build_amg_line_description(
    year_make_model: str | None,
    trim_body: str | None,
    option_packages: list[dict[str, Any]] | None,
) -> str | None:
    """Body-style-appropriate AMG Line Exterior Package description, or None
    when AMG Line isn't present at all. The G-Class gets its own description
    rather than the SUV fender-flare one — it has its own distinctive body
    design, and standard fender flare language does not apply to it.
    """
    packages = option_packages or []
    has_amg_line = any("AMG LINE" in str(p.get("name", "")).upper() for p in packages)
    if not has_amg_line:
        return None

    body_text = f"{str(trim_body or '').upper()} {str(year_make_model or '').upper()}"

    is_g_class = any(identifier in body_text for identifier in _AMG_LINE_G_CLASS_IDENTIFIERS)
    if is_g_class:
        return _AMG_LINE_G_CLASS_DESCRIPTION

    is_suv = any(model in body_text for model in _AMG_LINE_SUV_MODELS)
    is_sedan_coupe = any(model in body_text for model in _AMG_LINE_SEDAN_COUPE_MODELS)

    if "SUV" in body_text or "CROSSOVER" in body_text:
        is_suv = True
    if "SEDAN" in body_text or "COUPE" in body_text or "CABRIOLET" in body_text:
        is_sedan_coupe = True

    if is_suv:
        return _AMG_LINE_SUV_DESCRIPTION
    if is_sedan_coupe:
        return _AMG_LINE_SEDAN_COUPE_DESCRIPTION
    return _AMG_LINE_UNKNOWN_DESCRIPTION


_AMG_SYNONYM_PAIRS = [
    ("AMG BODY STYLING", "AMG LINE EXTERIOR"),
    ("AMG BODY STYLING", "AMG LINE EXTERIOR TREATMENT"),
    ("AMG EXTERIOR STYLING", "AMG LINE EXTERIOR"),
    ("AMG SPORT STYLING", "AMG BODY STYLING"),
]


def _norm_option_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "")).strip().upper()


def _is_amg_synonym_pair(a: str, b: str) -> bool:
    pair = {a, b}
    return any(pair == {x, y} for x, y in _AMG_SYNONYM_PAIRS)


def _shares_long_word_run(a: str, b: str) -> bool:
    """True when two normalized (already-uppercased) names are identical or
    share a run of more than 3 consecutive words."""
    if a == b:
        return True
    wa, wb = a.split(), b.split()
    for i in range(len(wa)):
        for j in range(len(wb)):
            k = 0
            while i + k < len(wa) and j + k < len(wb) and wa[i + k] == wb[j + k]:
                k += 1
            if k > 3:
                return True
    return False


def dedupe_equipment_descriptors(
    option_packages: list[dict[str, Any]] | None,
    standalone_options: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collapse redundant AMG descriptor phrases (and other near-duplicate
    names sharing more than 3 consecutive words) out of sub_items and
    standalone_options before they reach Claude. AutoiPacket's window-sticker
    parse frequently lists the same visual package under two or three
    overlapping phrasings (e.g. "AMG Body Styling" and "AMG Line Exterior
    Treatment" both describing one AMG Line Exterior Package) — left as-is,
    Claude reads these as separate features rather than one.

    Package NAMES (the priced top-level entries) are anchors and are never
    removed themselves — only sub_items within a package and
    standalone_options entries, the descriptive-duplicate layer this
    targets. Returns new (option_packages, standalone_options); the inputs
    are not mutated.

    Two different tie-break rules, matching the two checks:
    - AMG_SYNONYMS: the first entry encountered wins outright (packages are
      enumerated before their own sub_items and before standalone_options,
      so this naturally favors the priced package name — "the priced
      package entry" the spec calls out).
    - General >3-consecutive-word overlap: the longer (more descriptive)
      name wins regardless of encounter order, since neither side is
      necessarily the "official" package name.
    """
    packages = [
        dict(p, sub_items=list(p.get("sub_items") or []))
        for p in (option_packages or [])
    ]
    standalones = list(standalone_options or [])

    # (name, popper): popper is None for a package-level anchor (never
    # removed); otherwise a (container, item) pair passed to .remove() if
    # this entry loses a dedup match.
    entries: list[tuple[str, Any]] = []
    for pkg in packages:
        entries.append((pkg.get("name") or "", None))
        for si in pkg["sub_items"]:
            entries.append((si.get("name") or "", (pkg["sub_items"], si)))
    for s in standalones:
        entries.append((s.get("name") or "", (standalones, s)))

    # kept[normalized_name] = popper of the entry currently winning that slot.
    kept: dict[str, Any] = {}
    to_remove: list[Any] = []

    for name, popper in entries:
        norm = _norm_option_name(name)
        if not norm:
            continue

        synonym_match = next((k for k in kept if _is_amg_synonym_pair(k, norm)), None)
        if synonym_match is not None:
            if popper is not None:
                to_remove.append(popper)
            continue

        long_run_match = next((k for k in kept if _shares_long_word_run(k, norm)), None)
        if long_run_match is not None:
            existing_popper = kept[long_run_match]
            if existing_popper is None:
                # Existing is a package anchor — it always wins.
                if popper is not None:
                    to_remove.append(popper)
                continue
            # Both sides removable — the longer name wins.
            if len(norm) > len(long_run_match):
                to_remove.append(existing_popper)
                del kept[long_run_match]
                kept[norm] = popper
            elif popper is not None:
                to_remove.append(popper)
            continue

        kept[norm] = popper

    for popper in to_remove:
        container, item = popper
        try:
            container.remove(item)
        except ValueError:
            pass

    return packages, standalones


# --------------------------------------------------------------------------- #
# Recon gate
# --------------------------------------------------------------------------- #

RECON_INCOMPLETE_NOTE = (
    "ReconVision has no completed service items and 'Close RO' is incomplete — "
    "recon is still in progress. Wait and retry."
)

# --------------------------------------------------------------------------- #
# MB CPO data-completeness gate
# --------------------------------------------------------------------------- #

# Status 10 (MB Certified Pre-Owned) and 16 (Z-stock CPO) are the MB CPO tier.
# For these units ONLY, ACV Max pricing and the AutoiPacket window sticker must
# both have returned usable data before we spend a Claude call. Carfax and
# ReconVision are supplementary sources and never gate ad generation.
MB_CPO_GATE_STATUS_CODES = {10, 16}


def _mb_cpo_data_gate(
    stock: str,
    stock_prefix: str,
    status_code: int | None,
    pricing_raw: dict[str, Any],
    msrp_data: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Pre-API completeness check for MB CPO units (status 10/16).

    Requires:
      * ACV Max pricing — current_price present and > 0, and at least one proof
        point with direction "below".
      * AutoiPacket sticker — total_msrp present and > 0. Two escape hatches
        waive this without a real MSRP: source "acvmax_options_tab" (a
        Mercedes-Benz vehicle where AutoiPacket failed but the ACV Max options
        tab fallback succeeded — the gate only blocks when BOTH fail) and
        source "unavailable_after_retries" (a non-MB vehicle where AutoiPacket
        has failed 3 times and the pipeline gives up waiting on it).

    Returns a structured failure package (reason "incomplete_data" plus the
    failing source) when the gate fails, so the caller can skip the Claude API
    call. Returns None when the gate passes or the vehicle is not MB CPO.
    """
    if status_code not in MB_CPO_GATE_STATUS_CODES:
        return None

    current_price = pricing_raw.get("current_internet_price")
    proof_points = pricing_raw.get("pricing_proof_points") or []
    has_below = any(p.get("direction") == "below" for p in proof_points)
    acvmax_ok = (
        isinstance(current_price, (int, float))
        and current_price > 0
        and has_below
    )

    total_msrp = (msrp_data or {}).get("total_msrp")
    msrp_source = (msrp_data or {}).get("source")
    sticker_ok = (
        (isinstance(total_msrp, (int, float)) and total_msrp > 0)
        or msrp_source in ("acvmax_options_tab", "unavailable_after_retries")
    )

    if acvmax_ok and sticker_ok:
        return None

    failures: list[tuple[str, str]] = []
    if not acvmax_ok:
        failures.append(("acvmax_pricing", "ACV Max pricing unavailable"))
    if not sticker_ok:
        failures.append(("autoipacket_sticker", "window sticker missing"))

    message = "MB CPO data gate failed — " + "; ".join(m for _, m in failures)
    return {
        "recon_complete": True,
        "data_gate_passed": False,
        "reason": "incomplete_data",
        "failed_source": failures[0][0],
        "failed_sources": [name for name, _ in failures],
        "message": message,
        "stock_number": stock,
        "stock_prefix": stock_prefix,
        "status_code": status_code,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# Hendrick Certified (11), Hendrick Affordable (12), and As-Is (13) are not
# manufacturer CPO — a window sticker is optional for these tiers (many
# non-MB trade-ins never have one). ACV Max pricing is still required; without
# it there's no advertised_price and no proof point to write the ad from.
NON_CPO_GATE_STATUS_CODES = {11, 12, 13}


def _non_cpo_data_gate(
    stock: str,
    stock_prefix: str,
    status_code: int | None,
    pricing_raw: dict[str, Any],
) -> dict[str, Any] | None:
    """Pre-API completeness check for Hendrick Certified / Affordable / As-Is
    units (status 11/12/13). Requires only that ACV Max pricing returned a
    usable current_price — no window sticker requirement, unlike MB CPO.

    Returns a structured failure package (reason "incomplete_data") when the
    gate fails, so the caller can skip the Claude API call. Returns None when
    the gate passes or the vehicle isn't on one of these tiers.
    """
    if status_code not in NON_CPO_GATE_STATUS_CODES:
        return None

    current_price = pricing_raw.get("current_internet_price")
    if isinstance(current_price, (int, float)) and current_price > 0:
        return None

    message = "Non-CPO data gate failed — ACV Max pricing unavailable"
    return {
        "recon_complete": True,
        "data_gate_passed": False,
        "reason": "incomplete_data",
        "failed_source": "acvmax_pricing",
        "failed_sources": ["acvmax_pricing"],
        "message": message,
        "stock_number": stock,
        "stock_prefix": stock_prefix,
        "status_code": status_code,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _carfax_disqualifying_gate(
    stock: str,
    stock_prefix: str,
    status_code: int | None,
    carfax_raw: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Carfax reported a disqualifying event — a title brand (Salvage, Junk,
    Rebuilt, Fire, Flood, Hail, Lemon, Structural Damage, Not Actual Mileage,
    Exceeds Mechanical Limits) or an airbag deployment. Unlike an ordinary
    accident (which is disclosed in the ad, not blocked), this is the one case
    where damage stops ad generation outright, on every status code — no
    Claude call is made.

    Returns a structured failure package when the gate fails, so the caller
    can skip the Claude API call. Returns None when Carfax is unavailable,
    errored, or carries no disqualifying event.
    """
    cf = carfax_raw if isinstance(carfax_raw, dict) else {}
    if cf.get("error") or not cf.get("has_disqualifying_event"):
        return None

    found: list[str] = [f"title brand: {b}" for b in cf.get("title_brands") or []]
    if cf.get("airbag_deployed"):
        found.append("airbag deployment")
    message = (
        "DISQUALIFYING CARFAX EVENT — " + "; ".join(found)
        + ". Ad withheld. Human review required."
    )
    return {
        "recon_complete": True,
        "data_gate_passed": False,
        "reason": "disqualifying_carfax_event",
        "title_brands": cf.get("title_brands") or [],
        "airbag_deployed": bool(cf.get("airbag_deployed")),
        "message": message,
        "stock_number": stock,
        "stock_prefix": stock_prefix,
        "status_code": status_code,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _recon_is_complete(line_items: list[dict[str, Any]]) -> bool:
    """Recon is 'not complete' only when there are no completed service items AND
    the 'Close RO' step is itself incomplete."""
    has_completed_service = any(
        li.get("kind") == "service" and li.get("completed") for li in line_items
    )
    close_ro = next(
        (
            li
            for li in line_items
            if re.sub(r"[^a-z]", "", (li.get("section") or "").lower()) == "closero"
        ),
        None,
    )
    close_ro_incomplete = close_ro is not None and not close_ro.get("completed")
    return not (not has_completed_service and close_ro_incomplete)


def check_recon(
    stock_number: str,
    *,
    rv: ReconVisionScraper | None = None,
    headless: bool = True,
    fresh_login: bool = False,
) -> dict[str, Any]:
    """Light ReconVision-only check (no ACV MAX / Carfax / AutoiPacket). Returns
    {stock_number, recon_complete, work_order_id, note}.

    Pass an already-open `rv` (logged in) to reuse it across a per-vehicle loop
    instead of opening a new Chromium process and re-logging-in for every
    vehicle — see orchestrator.py's _run_inner()."""
    stock = stock_number.strip().lstrip("#").upper()
    owns_session = rv is None
    if owns_session:
        rv = ReconVisionScraper(headless=headless, use_saved_session=not fresh_login)
        rv.__enter__()
    try:
        rv.login(force=fresh_login)
        try:
            recon_raw = rv.scrape_work_order(stock)
        except WorkOrderLoadError:
            return {
                "stock_number": stock,
                "recon_complete": False,
                "note": "ReconVision timeout — will retry next run",
            }
    finally:
        if owns_session:
            rv.__exit__(None, None, None)
    complete = _recon_is_complete(recon_raw.get("line_items", []))
    return {
        "stock_number": stock,
        "recon_complete": complete,
        "work_order_id": recon_raw.get("work_order_id"),
        "note": None if complete else RECON_INCOMPLETE_NOTE,
    }


CERTIFICATION_PENDING_NOTE = (
    "Vehicle in inventory but not yet CPO certified in ACV Max — ad will "
    "generate automatically when certification status updates."
)


def check_certified(
    stock_number: str, *, headless: bool = True, fresh_login: bool = False
) -> dict[str, Any]:
    """Light ACV MAX pricing-only check (no ReconVision / Carfax / AutoiPacket).

    Not called anywhere currently (verified — no other module references
    check_certified). Originally: a vehicle can carry the MB CPO status code
    (10/16) in the DMS before ACV Max's own certification flag has caught up,
    so this let the orchestrator gate hold such a vehicle out of the build
    queue until ACV Max itself reported certified: True. That signal no
    longer exists — scrape_pricing() stopped reading anything
    certification-related off the pricing/inventory-row screen (it was
    misreading the "Certified" competitive-set filter, not the vehicle's own
    status; see its docstring and aggregator.CERTIFIED_STATUS_CODES). This
    function will now always resolve certified from status_code alone
    (bool(pricing_raw.get("certified")) is always False), same as
    CERTIFIED_STATUS_CODES — it no longer has a distinct signal to offer. If
    this gets revived, decide whether it should just call CERTIFIED_STATUS_CODES
    directly instead of scraping at all, or whether the ACV-Max-workflow-
    catch-up signal needs a new, real source first.

    Returns {stock_number, certified, note}."""
    stock = stock_number.strip().lstrip("#").upper()
    with ACVMaxScraper(headless=headless, use_saved_session=not fresh_login) as ax:
        ax.login(force=fresh_login)
        pricing_raw = ax.scrape_pricing(stock)
    certified = bool(pricing_raw.get("certified"))
    return {
        "stock_number": stock,
        "certified": certified,
        "note": None if certified else CERTIFICATION_PENDING_NOTE,
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


_EMPTY_RECON: dict[str, Any] = {
    "work_order_id": None,
    "line_items": [],
    "excluded_line_items": [],
    "all_tires_replaced": False,
    "scheduled_service_done": False,
    "brake_service_done": False,
    "wiper_blades_replaced": False,
    "spark_plugs_replaced": False,
    "air_filter_replaced": False,
    "oil_change_done": False,
    "pending": True,
}


def aggregate(
    stock_number: str,
    *,
    headless: bool = True,
    fresh_login: bool = False,
    skip_recon: bool = False,
    expected_vin: str | None = None,
    vehicle_id: str | None = None,
    bypass_rate_limits: bool = False,
) -> dict[str, Any]:
    """Run the scrapers for `stock_number` and return the unified package.

    `bypass_rate_limits=True` (the Flask /generate route only) skips the
    AutoiPacket business-hours / daily-count / minimum-delay gates on the live
    sticker pull, so an interactive request is never refused by them. It never
    skips the cache: the cached-sticker check runs first, unconditionally.
    Batch callers (orchestrator, CLI) leave it False and stay rate-limited.

    With `skip_recon=True` the ReconVision scraper and the recon-complete gate
    are skipped entirely (the "pre-recon" pipeline): the package still carries
    ACV MAX pricing, CTR, Carfax, and the AutoiPacket window sticker, `recon` is
    an empty stub, and the package is marked `recon_pending: True`.

    `expected_vin` (the VIN the inventory crawler has for this stock number, when
    available) is passed to the ACV MAX scraper as a secondary identity check on
    the pricing page — a stock-number or VIN mismatch raises VehicleIdentityError.

    `vehicle_id` (the ACV Max inventory id for this stock number, when a caller
    already has it — e.g. from a prior crawl snapshot) skips the ACV Max
    inventory search entirely: find_vehicle() is never called, and pricing is
    read straight from /inventory/{vehicle_id}/pricing. Without it, find_vehicle()
    is called exactly once, right here, and its result is handed to
    scrape_pricing() — a single search, never scrape_pricing()'s own internal
    one on top of it. See ACVMaxScraper.scrape_pricing()'s docstring for why a
    second independent search is a real race (it can resolve to a different
    row than the first).
    """
    stock = stock_number.strip().lstrip("#").upper()
    stock_prefix = stock[:2]
    is_z_stock = _is_z_stock(stock)
    use_saved = not fresh_login

    # --- 1-3b. main scraping block ------------------------------------- #
    #
    # Every scraper below opens its browser via a `with ... as x:` block, whose
    # _BrowserSession.__exit__ closes the context/browser/playwright driver in
    # its own try/finally — success or failure. The outer try/finally here is
    # a second guarantee on top of that: if a scraper ever leaves a context
    # open after a failure (e.g. an exception raised between context creation
    # and the `with` taking effect), gc.collect() below still forces Python to
    # drop the reference and release the underlying browser process promptly,
    # instead of letting it linger to the next vehicle.
    pricing_raw: dict[str, Any] = {}
    ctr_raw: dict[str, Any] = {}
    carfax_raw: dict[str, Any] = {}
    vin: str | None = expected_vin
    msrp_raw: dict[str, Any] | None = None
    # Per-source scraper_status, surfaced in the final package and in
    # format_data_package() so a human (or the ad writer) can see which
    # sources came from vehicle_cache.db vs. a live scrape this run. ACV Max
    # pricing is never cached (it changes daily and is the price/proof-point
    # source of truth), so it is always "scraped".
    sticker_status = "failed"
    carfax_status = "failed"
    recon_status = "skipped" if skip_recon else "failed"
    try:
        # --- 1. ReconVision first — it is the early-exit gate -------------- #
        #
        # ReconVision itself is keyed by stock number, not VIN, so a cache
        # check here only works when the inventory crawler already handed us
        # this vehicle's VIN via `expected_vin`. Without it we can't look the
        # vehicle up in vehicle_cache.db before scraping, so we scrape as
        # before and cache the result under `expected_vin` next time it's
        # available.
        recon_raw: dict[str, Any] = {}
        items: list[dict[str, Any]] = []
        if not skip_recon:
            if expected_vin and not needs_recon(expected_vin):
                recon_raw = get_recon(expected_vin) or {}
                items = recon_raw.get("line_items", [])
                recon_status = "cache_hit"
                print(
                    f"[cache] recon hit for VIN {expected_vin} — recon complete, "
                    f"skipping ReconVision",
                    file=sys.stderr,
                )
            else:
                with ReconVisionScraper(
                    headless=headless, use_saved_session=use_saved
                ) as rv:
                    rv.login(force=fresh_login)
                    recon_raw = rv.scrape_work_order(stock)

                # recon_image_bytes is in-process only (see ReconVisionScraper.
                # scrape_work_order()'s docstring note) — drop it before this
                # dict goes anywhere near json.dumps() (save_recon() below).
                recon_raw.pop("recon_image_bytes", None)

                items = recon_raw.get("line_items", [])
                # Completeness (Close RO / has-a-completed-service-item) is
                # decided from the raw DOM scrape, BEFORE the vision overlay
                # below — see _apply_recon_vision()'s docstring for why.
                recon_complete_now = _recon_is_complete(items)
                recon_status = "scraped" if recon_complete_now else "failed"

                # Vision-parse the freshly captured screenshot (if any) before
                # caching, so a later cache hit on this VIN reuses the
                # enriched result instead of paying for another vision call
                # on data that hasn't changed. Only line_items changes.
                recon_raw = _apply_recon_vision(recon_raw, recon_raw.get("vin") or expected_vin)
                items = recon_raw.get("line_items", items)

                if expected_vin:
                    save_recon(
                        expected_vin,
                        stock,
                        recon_raw,
                        recon_complete_now,
                        image_path=recon_raw.get("recon_image_path"),
                    )

                if not recon_complete_now:
                    return {
                        "recon_complete": False,
                        "stock_number": stock,
                        "stock_prefix": stock_prefix,
                        "is_z_stock": is_z_stock,
                        "work_order_id": recon_raw.get("work_order_id"),
                        "note": RECON_INCOMPLETE_NOTE,
                    }

        # --- 2. ACV MAX: pricing, then CTR + Carfax from the same open page # #
        # ACV Max pricing is always scraped fresh — never cached. It's the
        # source of truth for price changes and pricing proof points and it
        # changes daily.
        with ACVMaxScraper(headless=headless, use_saved_session=use_saved) as ax:
            ax.login(force=fresh_login)
            # Resolve the vehicle id exactly once — reuse a caller-supplied one
            # if we have it, otherwise search here and hand the result straight
            # to scrape_pricing() so it never runs its own, second, independent
            # search for the same stock number.
            resolved_vehicle_id = vehicle_id or ax.find_vehicle(stock)
            pricing_raw = ax.scrape_pricing(
                stock, vehicle_id=resolved_vehicle_id, expected_vin=expected_vin
            )
            vin = pricing_raw.get("vin") or vin
            try:
                ctr_raw = ax.scrape_ctr(pricing_raw.get("vehicle_id"))
            except ScraperError as exc:
                ctr_raw = {"error": str(exc)}

            if vin and not needs_carfax(vin):
                carfax_raw = get_carfax(vin) or {}
                carfax_status = "cache_hit"
                print(
                    f"[cache] Carfax hit for VIN {vin} — skipping scrape",
                    file=sys.stderr,
                )
            else:
                try:
                    carfax_raw = ax.scrape_carfax(
                        acvmax_mileage=pricing_raw.get("mileage")
                    )
                    if not carfax_raw.get("error"):
                        carfax_status = "scraped"
                        if vin:
                            # Vision-parse the freshly captured screenshot
                            # (if any) before caching, so a later cache hit
                            # on this VIN reuses the enriched result instead
                            # of paying for another vision call on data that
                            # hasn't changed.
                            carfax_raw = _apply_carfax_vision(carfax_raw, vin)
                            save_carfax(
                                vin,
                                stock,
                                carfax_raw,
                                image_path=carfax_raw.get("carfax_image_path"),
                            )
                except (CarfaxError, ScraperError) as exc:
                    carfax_raw = {"error": str(exc)}
                    stale = (get_vehicle(vin) or {}) if vin else {}
                    stale_json = stale.get("carfax_json")
                    if stale_json:
                        carfax_raw = json.loads(stale_json)
                        carfax_status = "cache_hit"
                        print(
                            f"[cache] Carfax scrape failed — using cached data "
                            f"for {vin}",
                            file=sys.stderr,
                        )

        # Carfax disqualifying-event gate — a title brand or an airbag
        # deployment stops ad generation outright, on every status code. An
        # ordinary accident (no disqualifying event) is not gated here; it is
        # disclosed in the ad instead, per the CARFAX DATA RULES in
        # SYSTEM_PROMPT. Checked before the AutoiPacket sticker pull so a
        # disqualified vehicle never spends that call either.
        disqualified = _carfax_disqualifying_gate(
            stock, stock_prefix, pricing_raw.get("status_code"), carfax_raw
        )
        if disqualified is not None:
            print(
                f"[aggregator] {stock}: {disqualified['message']} — "
                f"skipping the Claude API call",
                file=sys.stderr,
            )
            return disqualified

        # --- 3. Window sticker / equipment sourcing ------------------------ #
        # Mercedes-Benz: cached sticker -> live AutoiPacket pull -> Carfax
        # sticker link (3b) -> ACV Max options tab (3c). Unchanged.
        # Everything else: cached sticker -> Carfax sticker link -> ACV Max
        # options tab -> live AutoiPacket pull as the LAST resort, and only
        # then the bounded retry/give-up count (3c). Checked in that order so
        # a non-MB trade-in AutoiPacket does not cover never burns a pull (and
        # a retry attempt) on a VIN we can already source another way.
        make = _make_from_ymm(pricing_raw.get("year_make_model"))
        is_mb = bool(make) and "mercedes" in make.lower()

        def _msrp_unusable(m: Any) -> bool:
            # AutoiPacket can "succeed" (no error key) but still parse out to
            # nothing usable for a VIN it doesn't cover, so treat that the
            # same as an outright error.
            return bool(
                not m
                or m.get("error")
                or (m.get("total_msrp") is None and not m.get("option_packages"))
            )

        def _carfax_link_sticker() -> dict[str, Any] | None:
            """Parse and cache the OEM sticker Carfax's report links to, if
            it links one. None when there is no link or the parse fails."""
            sticker_url = (carfax_raw or {}).get("window_sticker_url")
            if not sticker_url:
                return None
            try:
                fallback = _parse_oem_sticker(sticker_url, vin, make)
            except (StickerNotFoundError, ScraperError) as exc:
                print(
                    f"[aggregator] Carfax sticker-link fallback failed for {vin}: {exc}",
                    file=sys.stderr,
                )
                return None
            save_window_sticker(
                vin,
                stock,
                pricing_raw.get("year_make_model"),
                fallback,
                _sticker_source(fallback),
                image_path=fallback.get("sticker_image_path"),
            )
            _capture_sticker_to_rarity(
                vin, fallback, pricing_raw, source="carfax_sticker_link"
            )
            print(
                f"[aggregator] recovered MSRP data for {vin} via the Carfax "
                f"sticker link ({sticker_url})"
            )
            return fallback

        def _options_tab_msrp() -> dict[str, Any] | None:
            """Approximate MSRP from the packages ACV Max has on file for this
            vehicle (no real MSRP, but the vehicle's actual selected packages
            plus an approximate, non-OEM price per package from the Find
            Packages catalog). None when the scrape fails."""
            options_vehicle_id = pricing_raw.get("vehicle_id") or vehicle_id
            try:
                with ACVMaxScraper(
                    headless=headless, use_saved_session=use_saved
                ) as ax2:
                    ax2.login(force=fresh_login)
                    options = ax2.scrape_options_tab(options_vehicle_id)
            except ScraperError as exc:
                print(
                    f"[aggregator] ACV Max options tab fallback failed for "
                    f"{vin}: {exc}",
                    file=sys.stderr,
                )
                return None
            selected = options.get("selected_packages") or []
            approx_prices = [
                p["approx_msrp"] for p in selected if p.get("approx_msrp") is not None
            ]
            return {
                "source": "acvmax_options_tab",
                "vehicle_id": options.get("vehicle_id"),
                "base_price": None,
                "total_msrp": sum(approx_prices) if approx_prices else None,
                "option_packages": [
                    {
                        "code": p.get("code"),
                        "name": p.get("name"),
                        "price": p.get("approx_msrp"),
                        "description": p.get("description"),
                    }
                    for p in selected
                ],
                "selected_packages": selected,
                "exterior_color": options.get("exterior_color"),
                "interior_color": options.get("interior_color"),
                "scraped_at": options.get("scraped_at"),
                "msrp_note": (
                    "MSRP approximate — from ACV Max option packages, "
                    "not OEM window sticker"
                ),
            }

        msrp_raw = None
        skip_live_pull = False
        if not vin:
            msrp_raw = {"error": "no VIN from ACV MAX; cannot pull the window sticker"}
            skip_live_pull = True
        elif not needs_window_sticker(vin):
            msrp_raw = get_window_sticker(vin)
            sticker_status = "cache_hit"
            skip_live_pull = True
            print(
                f"[cache] window sticker hit for VIN {vin} — skipping AutoiPacket",
                file=sys.stderr,
            )
        elif not is_mb:
            # 3a. Carfax window-sticker link, before any live AutoiPacket pull.
            link = _carfax_link_sticker()
            if link is not None and not _msrp_unusable(link):
                msrp_raw = link
                sticker_status = "scraped"
                skip_live_pull = True
                print(f"[aggregator] non-MB sticker source for {vin}: Carfax sticker link")
            else:
                # 3a'. ACV Max options tab, before any live AutoiPacket pull.
                opts = _options_tab_msrp()
                if opts is not None and not _msrp_unusable(opts):
                    msrp_raw = opts
                    sticker_status = "options_tab_fallback"
                    skip_live_pull = True
                    # Cached like a normal sticker pull (source label
                    # "acvmax_options_tab" rides along in the stored JSON) so
                    # later generations skip the ACV Max browser session.
                    save_window_sticker(
                        vin,
                        stock,
                        pricing_raw.get("year_make_model"),
                        msrp_raw,
                        _sticker_source(msrp_raw),
                        image_path=msrp_raw.get("sticker_image_path"),
                    )
                    print(
                        f"[aggregator] non-MB sticker source for {vin}: ACV Max "
                        f"options tab (no usable Carfax sticker link)"
                    )
                else:
                    attempts_so_far = get_autoipacket_attempts(vin)
                    if attempts_so_far >= 3:
                        msrp_raw = {
                            "source": "unavailable_after_retries",
                            "total_msrp": None,
                            "option_packages": [],
                        }
                        sticker_status = "unavailable_after_retries"
                        skip_live_pull = True
                        print(
                            f"[aggregator] non-MB {vin}: already at {attempts_so_far} "
                            f"failed AutoiPacket attempts — skipping live pull"
                        )
                    else:
                        print(
                            f"[aggregator] non-MB {vin}: no usable Carfax sticker link or "
                            f"ACV Max options — falling back to live AutoiPacket pull"
                        )

        # The non-MB rate limiters (11am-6pm window, 5-minute spacing, 15/day
        # downloads, 10/day manual pulls) and the MB hours/daily limit all live
        # inside pull_sticker_endpoint() — tier 3, the live download — so the
        # rarity-cache and browse tiers are never gated. bypass_rate_limits is
        # passed through for the Flask route; non_mb selects the stricter gates.
        if not skip_live_pull:
            with AutoiPacketScraper(
                headless=headless, use_saved_session=use_saved
            ) as ap:
                ap.login(force=fresh_login)
                try:
                    msrp_raw = ap.pull_sticker(
                        vin, bypass_rate_limits=bypass_rate_limits, non_mb=not is_mb
                    )
                except ScraperError as exc:
                    msrp_raw = {"error": str(exc)}

            if msrp_raw and not msrp_raw.get("error"):
                sticker_status = "scraped"
                save_window_sticker(
                    vin,
                    stock,
                    pricing_raw.get("year_make_model"),
                    msrp_raw,
                    _sticker_source(msrp_raw),
                    image_path=msrp_raw.get("sticker_image_path"),
                )
            else:
                stale = get_vehicle(vin) or {}
                stale_json = stale.get("window_sticker_json")
                if stale_json:
                    msrp_raw = json.loads(stale_json)
                    sticker_status = "cache_hit"
                    print(
                        f"[cache] AutoiPacket failed — using stale window "
                        f"sticker for {vin}",
                        file=sys.stderr,
                    )
            _capture_sticker_to_rarity(vin, msrp_raw, pricing_raw)

            # Held back by a non-MB rate limit (pull_sticker_endpoint returned
            # its rate_limited marker): not a failed pull, so it must not burn
            # one of the 3 retry attempts below.
            rate_limited = bool(msrp_raw and msrp_raw.get("rate_limited"))
            if rate_limited:
                sticker_status = "rate_limited"
                print(
                    f"[aggregator] non-MB {vin}: live pull held back by a rate "
                    f"limit — {msrp_raw.get('error')}"
                )

            # A live pull that actually ran and came back unusable is the only
            # thing that counts as a failed attempt (non-MB only). The
            # pre-check above stops the pull once attempts reach 3.
            if not is_mb and not rate_limited and _msrp_unusable(msrp_raw):
                increment_autoipacket_attempts(vin)
                attempts = get_autoipacket_attempts(vin)
                print(
                    f"[aggregator] AutoiPacket failed for non-MB vehicle — "
                    f"attempt {attempts} of 3"
                )
                if attempts >= 3:
                    msrp_raw = {
                        "source": "unavailable_after_retries",
                        "total_msrp": None,
                        "option_packages": [],
                    }
                    sticker_status = "unavailable_after_retries"

        # --- 3b. Carfax window-sticker fallback -- Mercedes-Benz only (non-MB
        # vehicles already tried the Carfax link above, before AutoiPacket).
        # Only when AutoiPacket came back empty/errored and Carfax's own
        # report happened to link an OEM sticker.
        if vin and is_mb and _msrp_unusable(msrp_raw):
            link = _carfax_link_sticker()
            if link is not None:
                msrp_raw = link
                sticker_status = "scraped"

        # --- 3c. Final fallback -- Mercedes-Benz only, reached when AutoiPacket
        # and the Carfax link both left msrp_raw unusable: the ACV Max options
        # tab. (Non-MB vehicles already tried the options tab before the live
        # pull, and their bounded retry count lives with that pull above.)
        if vin and is_mb and _msrp_unusable(msrp_raw):
            opts = _options_tab_msrp()
            if opts is not None:
                msrp_raw = opts
                sticker_status = "options_tab_fallback"
                print(
                    "[aggregator] AutoiPacket failed for MB vehicle — using "
                    "ACV Max options tab fallback"
                )
    finally:
        gc.collect()
        print(f"[aggregator] browser cleanup after {stock}")

    # --- 4. assemble the unified package ------------------------------- #
    msrp_data = _msrp_data(msrp_raw)
    msrp_data = _apply_sticker_vision(msrp_data, msrp_raw, vin)
    msrp_data = _filter_feature_noise(msrp_data)

    # Data-completeness gates before we spend a Claude call. Carfax /
    # ReconVision failures never block here, on any tier.
    #   status 10/16 (MB CPO): ACV Max pricing AND the AutoiPacket window
    #     sticker must both have returned usable data.
    #   status 11/12/13 (Hendrick Certified/Affordable, As-Is): ACV Max
    #     pricing only — a window sticker is optional on these tiers.
    gate_failure = _mb_cpo_data_gate(
        stock, stock_prefix, pricing_raw.get("status_code"), pricing_raw, msrp_data
    ) or _non_cpo_data_gate(stock, stock_prefix, pricing_raw.get("status_code"), pricing_raw)
    if gate_failure is not None:
        print(
            f"[aggregator] {stock}: {gate_failure['message']} — "
            f"skipping the Claude API call",
            file=sys.stderr,
        )
        return gate_failure

    recon_block = (
        dict(_EMPTY_RECON)
        if skip_recon
        else {
            "work_order_id": recon_raw.get("work_order_id"),
            **_filter_recon(items, status_code=pricing_raw.get("status_code") or 10),
        }
    )
    warranty_sentence = build_warranty_sentence(
        carfax_raw,
        pricing_raw,
        pricing_raw.get("status_code"),
        pricing_raw.get("year_make_model"),
    )
    provenance_sentence = build_provenance_sentence(
        stock, carfax_raw, pricing_raw.get("status_code")
    )
    proof_point_sentence, proof_point_type = build_proof_point_sentence(
        pricing_raw,
        _advertised_price(pricing_raw),
        current_price=pricing_raw.get("current_internet_price"),
        search_distance=pricing_raw.get("search_distance"),
    )
    return {
        "recon_complete": True,
        "recon_pending": skip_recon,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stock_number": stock,
        "stock_prefix": stock_prefix,
        "is_z_stock": is_z_stock,
        "vehicle": {
            **_vehicle(pricing_raw),
            **_resolve_colors(pricing_raw, msrp_data, msrp_raw),
        },
        "msrp_data": msrp_data,
        "pricing": _pricing(
            pricing_raw.get("pricing_proof_points", []),
            pricing_raw.get("current_internet_price"),
        ),
        "market_velocity": _market_velocity(pricing_raw),
        "ctr": ctr_raw,
        "carfax": _carfax(carfax_raw),
        "provenance_sentence": provenance_sentence,
        "carfax_sentence": build_carfax_sentence(
            carfax_raw,
            pricing_raw.get("status_code"),
            provenance_sentence=provenance_sentence,
        ),
        "recon_sentence": build_recon_sentence(
            recon_block, pricing_raw.get("status_code"), pricing_raw.get("mileage")
        ),
        "proof_point_sentence": proof_point_sentence,
        "proof_point_type": proof_point_type,
        "msrp_sentence": build_msrp_sentence(
            (msrp_data or {}).get("total_msrp"),
            _advertised_price(pricing_raw),
            pricing_raw.get("status_code"),
            pricing_raw.get("year_make_model"),
        ),
        "warranty_sentence": warranty_sentence,
        "shipping_sentence": build_shipping_sentence(
            pricing_raw,
            msrp_data,
            pricing_raw.get("status_code"),
            pricing_raw.get("year_make_model"),
            warranty_sentence=warranty_sentence,
        ),
        "amg_line_description": build_amg_line_description(
            pricing_raw.get("year_make_model"),
            pricing_raw.get("trim_body"),
            (msrp_data or {}).get("option_packages", []),
        ),
        "recon": recon_block,
        "scraper_status": {
            "autoipacket": sticker_status,
            "carfax": carfax_status,
            "reconvision": recon_status,
            "acvmax": "scraped",  # never cached — always fresh
        },
        **_peacock_fields(msrp_data, pricing_raw.get("status_code")),
        **_warranty_fields(pricing_raw, carfax_raw),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the unified ad-writer data package for a stock number"
    )
    parser.add_argument("stock", help="dealership stock number, e.g. DT23358A")
    parser.add_argument(
        "--headed", action="store_true", help="show the browser windows"
    )
    parser.add_argument(
        "--fresh-login",
        action="store_true",
        help="ignore all saved sessions and log in fresh everywhere",
    )
    parser.add_argument(
        "--skip-recon",
        action="store_true",
        help="pre-recon pipeline: skip ReconVision and the recon-complete gate",
    )
    args = parser.parse_args(argv)

    try:
        package = aggregate(
            args.stock,
            headless=not args.headed,
            fresh_login=args.fresh_login,
            skip_recon=args.skip_recon,
        )
    except ScraperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(package, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
