#!/usr/bin/env python3
"""scraper.py — browser automation for the ad writer tool.

All Playwright-based scraping lives here. Each source site gets its own scraper
class:

  * AutoiPacketScraper  — OEM window sticker / "Sticker Pull", keyed by VIN.
  * ACVMaxScraper       — ACV MAX pricing screen (proof points etc.), keyed by
                          stock number; scrape_carfax() also opens the linked
                          Carfax VHR in a new tab and scrapes it.
  * ReconVisionScraper  — work-order line items, keyed by stock number.

Quick test from the command line:

    python scraper.py <VIN>                          # AutoiPacket (default)
    python scraper.py --source acvmax <STOCK#>       # ACV MAX pricing
    python scraper.py --source reconvision <STOCK#>  # ReconVision work order
    python scraper.py <VIN> --headed --keep-open

Credentials are read from credentials.py (git-ignored).

AutoiPacket flow (verified against the live site):
  1. Log in at auth.autoipacket.com  (email + password form).
  2. Click "Dealer Portal" on the account page — SSO handoff that establishes the
     dpapp.autoipacket.com session. A direct hit on /stickerpull without this
     step bounces to dpapp.../login.
  3. dpapp.autoipacket.com/stickerpull — type VIN into #stickerpull-input,
     click button.stickerpull-submit.
  4. The sticker renders in an <iframe> from
     document-viewer.autoipacket.com/sticker/<VIN>?token=... — scrape that frame.

ACV MAX flow (verified against the live site):
  1. CAS login at auth.firstlook.biz/cas/login (#username / #password / submit).
  2. Lands on max.firstlook.biz/fl-ims/secured/index — a dealer list. Click
     "Mercedes-Benz of Durham"; that SSO-hops to my.max.auto/inventory.
  3. my.max.auto/inventory (Vuetify SPA) — press "/" to focus Quick Search, type
     the stock number, click the single result row -> /inventory/<id>.
  4. Navigate to /inventory/<id>/pricing. The pricing UI is an <iframe> from
     max.firstlook.biz/merchandising/PricingAnalysis/pingone?inv=<id>... — the
     vehicle header and "3 Pricing Proof Points" section live in that frame.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

import credentials
from run_lock import SCRAPER_LOCK_PATH, acquire_scraper_lock, release_scraper_lock

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# AutoiPacket
AUTH_URL = "https://auth.autoipacket.com"
ACCOUNT_URL = "https://auth.autoipacket.com/account"
STICKER_PULL_URL = "https://dpapp.autoipacket.com/stickerpull"
PACKETS_URL = "https://dpapp.autoipacket.com/packets"
STICKER_FRAME_HINT = "document-viewer.autoipacket.com/sticker"
# Non-Mercedes VINs render the sticker as a PDF (react-pdf, div.module-pdf)
# instead of the document-viewer iframe. The PDF is fetched from this endpoint.
STICKER_PDF_RENDER_SELECTOR = "div.module-pdf, .react-pdf__Page"
STICKER_PDF_DOWNLOAD_HINT = "sticker-puller/download/"

# Rate limiting for the /stickerpull endpoint specifically (pull_sticker_endpoint()
# below) — NOT the packets-browse tiers (pull_sticker() tiers 1/2, sticker_from_packet(),
# _sticker_via_browse(), crawl_ipacket_inventory()), which are unaffected.
IPACKET_DAILY_COUNT_FILE = Path(__file__).with_name("ipacket_daily_count.json")
IPACKET_DAILY_LIMIT = 20
IPACKET_BUSINESS_HOUR_START = 8  # 8:00am local
IPACKET_BUSINESS_HOUR_END = 18  # 6:00pm local
IPACKET_DELAY_RANGE_SECONDS = (45, 90)

# Rate limiting for live non-MB sticker pulls made by aggregate() (batch
# callers only — the Flask route bypasses both). (crawl_ipacket_inventory()
# is separate and still skips every non-MB VIN outright via _is_mercedes_vin().)
# Deliberately stricter than the MB-only IPACKET_* limiter above: a lower
# daily cap, a narrower window, and a real minimum delay between downloads
# rather than just a random pre-download sleep.
#
# Two independent counters, both checked before a non-MB live pull:
#   * NON_MB_STICKER_DAILY_LIMIT           — "manual pulls" per day
#   * NON_MB_STICKER_DOWNLOAD_DAILY_LIMIT  — sticker file downloads per day
# Same window and minimum spacing, separate count files and separate limits.
NON_MB_STICKER_DAILY_COUNT_FILE = Path(__file__).with_name("non_mb_sticker_daily_count.json")
NON_MB_STICKER_DAILY_LIMIT = 10
NON_MB_STICKER_DOWNLOAD_DAILY_LIMIT = 15
NON_MB_STICKER_DOWNLOAD_COUNT_FILE = Path(__file__).with_name("non_mb_sticker_download_count.json")
# One shared "last non-MB pull attempt at" timestamp for the spacing check,
# written on every real non-MB tier-3 attempt (Flask included).
NON_MB_STICKER_LAST_PULL_FILE = Path(__file__).with_name("non_mb_sticker_last_pull.json")
NON_MB_STICKER_BUSINESS_HOUR_START = 11  # 11:00am local
NON_MB_STICKER_BUSINESS_HOUR_END = 18  # 6:00pm local
NON_MB_STICKER_MIN_DELAY_SECONDS = 300  # 5 minutes between downloads

# ACV MAX
ACVMAX_CAS_LOGIN_URL = (
    "https://auth.firstlook.biz/cas/login"
    "?service=https%3A%2F%2Fmax.firstlook.biz%2Ffl-ims%2Flogin%2Fcas"
)
ACVMAX_DEALER_INDEX_URL = "https://max.firstlook.biz/fl-ims/secured/index"
ACVMAX_INVENTORY_URL = "https://my.max.auto/inventory"
ACVMAX_DEALERSHIP = "Mercedes-Benz of Durham"
ACVMAX_PRICING_FRAME_HINT = "merchandising/PricingAnalysis"
# Confirmed from live debug dumps (scraper_debug/*-acvmax-pricing-frame-*.html
# and the *-acvmax-no-options-*/-no-equipment-* failures that motivated this
# fix): the Options and Equipment tabs render inside their own merchandising
# iframe too, just like pricing — the outer /inventory/{id}/<tab> page is an
# empty shell. Each tab's iframe src is a distinct Workflow/*.aspx endpoint,
# not a shared one, so each needs its own hint.
ACVMAX_OPTIONS_FRAME_HINT = "merchandising/Workflow/LoadPackages"
ACVMAX_EQUIPMENT_FRAME_HINT = "merchandising/Workflow/LoadEquipment"

# ReconVision
RECONVISION_SIGNIN_URL = "https://app.reconvision.com/user/sign_in"
RECONVISION_HOME_URL = "https://app.reconvision.com/"

DEFAULT_TIMEOUT_MS = 30_000
DEBUG_DIR = Path(__file__).with_name("scraper_debug")

# Vision-parsing image caches (see vision_parser.py). Screenshots/renders are
# best-effort — a capture failure here never blocks the scrape it's attached
# to, it just means that VIN falls back to text/regex parsing.
CARFAX_CACHE_DIR = Path(__file__).with_name("carfax_cache")
STICKER_CACHE_DIR = Path(__file__).with_name("sticker_cache")
RECON_CACHE_DIR = Path(__file__).with_name("recon_cache")
CARFAX_CACHE_DIR.mkdir(exist_ok=True)
STICKER_CACHE_DIR.mkdir(exist_ok=True)
RECON_CACHE_DIR.mkdir(exist_ok=True)

# Site-specific selectors, first-visible-wins lists. All confirmed against the
# live sites; adjust here if the markup changes.
SELECTORS: dict[str, list[str]] = {
    # --- AutoiPacket login (auth.autoipacket.com) ---
    "login_email": [
        "input[type='email']",
        "input[name='email']",
        "input[name='username']",
        "#email",
    ],
    "login_password": [
        "input[type='password']",
        "input[name='password']",
        "#password",
    ],
    "login_submit": [
        "button[type='submit']",
        "button:has-text('Sign In')",
        "button:has-text('Log In')",
        "button:has-text('Login')",
        "input[type='submit']",
    ],
    "logged_in_marker": [
        ".auth-header-account-button",
        "[class*='account-button'][title*='@']",
        "a:has-text('Dealer Portal')",
        "text=Welcome to the iPacket Account Page",
    ],
    "dealer_portal_link": [
        "a:has-text('Dealer Portal')",
        "a[href^='https://dpapp.autoipacket.com/']",
    ],
    # --- AutoiPacket Sticker Pull (dpapp.autoipacket.com/stickerpull) ---
    "vin_input": [
        "#stickerpull-input",
        "input[placeholder*='VIN' i]",
        "input.stickerpull-input",
    ],
    "vin_submit": [
        "button.stickerpull-submit",
        "button[type='submit']:has-text('Submit')",
        "button:has-text('Submit')",
    ],
    "dpapp_login_marker": [
        "input[type='password']",
        "form[action*='login' i]",
    ],
    # --- ACV MAX CAS login (auth.firstlook.biz) ---
    "acvmax_username": ["#username", "input[name='username']"],
    "acvmax_password": ["#password", "input[name='password']"],
    "acvmax_login_submit": [
        "button[name='submit']",
        "button:has-text('LOG IN')",
        "button[type='submit']",
        "input[type='submit']",
    ],
    "acvmax_cas_login_marker": ["#password", "input[name='execution']"],
    "acvmax_dealer_link": [f'a:has-text("{ACVMAX_DEALERSHIP}")'],
    # --- ACV MAX inventory SPA (my.max.auto) ---
    "acvmax_inventory_ready": [
        "text=Add a Filter",
        "text=Quick Search",
        "text=vehicles",
    ],
    "acvmax_quick_search": [
        "input.v-field__input",
        "input[type='text']",
    ],
    # --- ACV MAX pricing iframe (max.firstlook.biz/merchandising/PricingAnalysis) ---
    "acvmax_proof_points_ready": [
        "text=Pricing Proof Points",
    ],
    # --- ReconVision (app.reconvision.com) ---
    # The two-step login is confirmed against the live page. Everything below the
    # "# UNVERIFIED" marker is a best-effort guess (no test password available)
    # and will likely need one tuning pass against the live site.
    "rv_username": [
        "#user_username",
        "input[name='user[username]']",
        "input[placeholder='Username' i]",
    ],
    "rv_next": [
        "#next-button",
        "button:has-text('Next')",
        "button:has-text('Continue')",
    ],
    "rv_password": [
        "#user_password",
        "input[name='user[password]']",
        "input[type='password']",
        "input[placeholder='Password' i]",
    ],
    "rv_signin": [
        "input[type='submit'][name='commit']",
        "input[type='submit']",
        "#sign-in-button",
        "button[type='submit']",
        "button:has-text('Sign In')",
    ],
    "rv_signin_form_marker": [
        "#user_username",
        "#user_password",
        "input[name='user[username]']",
    ],
    # UNVERIFIED --------------------------------------------------------------
    "rv_home_marker": [
        "input[type='search']",
        "input[placeholder*='Search' i]",
        "a[href*='/work_orders']",
        "text=Work Orders",
    ],
    "rv_search": [
        "input[type='search']",
        "input[placeholder*='Search' i]",
        "input[name*='search' i]",
        "#search",
        ".search input",
        "header input[type='text']",
    ],
}

# AutoiPacket sticker section headers — Mercedes-Benz (HTML) layout.
SEC_STANDARD = "STANDARD OPTIONS"
SEC_BASE = "BASE SUGGESTED PRICE"
SEC_ADDED = "ADDED OPTIONS"
SEC_PRICE_DETAILS = "PRICE DETAILS"
SEC_TOTAL = "TOTAL SUGGESTED PRICE"

_CHROME_LINES = {"Single Page", "Multi Page", "Text Only", ""}

_MONEY_LINE = re.compile(r"^\$?\s*[\d,]+(?:\.\d{2})?$")
_OPTION_LINE = re.compile(r"^([0-9A-Z]{2,4})\s+-\s+(.+)$")
_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{11,17}")

# Mercedes-Benz VIN manufacturer/make prefixes. W1 and WDx are MB's German
# (Sindelfingen/Bremen/...) WMI codes; 4JG is MB USA's domestic-assembly code
# (used for U.S.-built GLE/GLS/GLC etc., e.g. 4JGFB4FB4RB182965 in the GLE 350
# example ad above). Any VIN not starting with one of these is non-MB.
_MB_VIN_PREFIXES = ("W1", "WDB", "WDD", "WDC", "4JG")


def _is_mercedes_vin(vin: str | None) -> bool:
    """True if `vin`'s manufacturer prefix corresponds to Mercedes-Benz."""
    v = (vin or "").strip().upper()
    return bool(v) and v.startswith(_MB_VIN_PREFIXES)

# An option/package line naming an interior material (Leather / MB-Tex /
# Nappa). Used by aggregator._interior_from_options() as the interior-color
# fallback when neither ACV Max nor the sticker's INTERIOR header field
# carried one — MB stickers often declare the interior as a paid option line
# rather than a header field.
_INTERIOR_MATERIAL_RE = re.compile(r"\b(?:leather|mb-tex|nappa)\b", re.IGNORECASE)

# Non-Mercedes (PDF) sticker: "*Cargo Package $225.00" style added-feature line.
_NONMB_ADDED_LINE = re.compile(r"^\*?\s*(.+?)\s+\$([\d,]+(?:\.\d{2})?)\s*$")
_NONMB_DRIVETRAIN = r"FWD|AWD|4WD|RWD|2WD|4MATIC|XDRIVE|4X4|4X2"

# Non-Mercedes sticker: known ALL CAPS category headers that group standard
# features under STANDARD FEATURES (e.g. Hyundai's "ADVANCED SAFETY
# TECHNOLOGY" / "POWERTRAIN TECHNOLOGY" / "COMFORT & CONVENIENCE" blocks).
# These are category labels, never priced packages — see _is_nonmb_section_header().
_NONMB_SECTION_HEADER_WORDS = (
    "ADVANCED SAFETY",
    "POWERTRAIN TECHNOLOGY",
    "COMFORT & CONVENIENCE",
    "COMFORT AND CONVENIENCE",
    "STANDARD FEATURES",
    "ADDED FEATURES",
    "EXTERIOR",
    "INTERIOR",
)
_NONMB_SECTION_HEADER_RE = re.compile(
    r"^(?:" + "|".join(re.escape(w) for w in _NONMB_SECTION_HEADER_WORDS) + r")\b",
    re.IGNORECASE,
)
# Generic ALL CAPS heading fallback for makes that use category names outside
# the known list above: no lowercase letters, no price, and not a STD/INCLUDED
# standard-feature line (those are handled separately).
_NONMB_ALL_CAPS_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9&/'\-\s]*[A-Z0-9]$")


def _is_nonmb_section_header(line: str) -> bool:
    """True if `line` looks like a non-MB sticker's ALL CAPS category header
    (e.g. "ADVANCED SAFETY TECHNOLOGY") rather than a real feature or priced
    package line. Category headers group standard equipment; they are never
    packages and must not be attributed prices or treated as sub-items."""
    ln = line.strip()
    if not ln or "$" in ln:
        return False
    if _NONMB_SECTION_HEADER_RE.match(ln):
        return True
    if re.search(r"\b(STD|INCLUDED)\b", ln, re.IGNORECASE):
        return False
    return bool(_NONMB_ALL_CAPS_HEADER_RE.match(ln))


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ScraperError(RuntimeError):
    """Base class for scraper failures."""


class LoginError(ScraperError):
    """Login did not complete successfully."""


class StickerNotFoundError(ScraperError):
    """The window sticker never rendered for the given VIN."""


class VehicleNotFoundError(ScraperError):
    """The stock number did not resolve to exactly one inventory vehicle."""


class PricingNotFoundError(ScraperError):
    """The ACV MAX pricing screen never rendered."""


class WorkOrderNotFoundError(ScraperError):
    """The stock number did not resolve to exactly one ReconVision work order."""


class VehicleIdentityError(ScraperError):
    """The ACV Max pricing page that loaded doesn't match the vehicle that was
    searched for — a resolved stock number or VIN mismatch. Raised instead of
    silently returning another vehicle's pricing data."""


class WorkOrderLoadError(ScraperError):
    """A resolved work-order id was found, but its edit page failed to load
    (a transient ReconVision timeout) — distinct from WorkOrderNotFoundError,
    which means the search itself came up empty. Retryable next run."""


class CarfaxError(ScraperError):
    """The Carfax report link could not be opened or read."""


class OptionsTabError(ScraperError):
    """The ACV Max Options tab never rendered a Selected Packages section, or
    none could be parsed — the MB CPO fallback when AutoiPacket can't produce
    a sticker."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _money(text: str | None) -> float | None:
    """Parse '$47,100.00' / '47100' / 'N/A' -> 47100.0 / None."""
    if not text:
        return None
    m = re.search(r"-?\$?\s*([\d,]+(?:\.\d{1,2})?)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _int(text: str | None) -> int | None:
    v = _money(text)
    return int(v) if v is not None else None


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _clean_nonmb_color(raw: str | None) -> str | None:
    """Clean a non-MB sticker color value. Some makes duplicate the value
    around a slash (e.g. "LIGHT BEIGE/LIGHT BEIGE" for matching seat/interior
    trim) — collapse that to the single value. Title-cases the ALL CAPS PDF
    text for consistency with how colors are written elsewhere in the ad."""
    v = _clean(raw)
    if not v:
        return None
    if "/" in v:
        parts = [p.strip() for p in v.split("/")]
        if len(parts) == 2 and parts[0].lower() == parts[1].lower():
            v = parts[0]
    return v.title()


_US_STATES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming",
    "District of Columbia",
}


def _slice_between(text: str, start: str, ends: tuple[str, ...]) -> str:
    """Substring of `text` from just after the first `start` to the first of any
    marker in `ends` that follows it (or end of string)."""
    low = text.lower()
    i = low.find(start.lower())
    if i < 0:
        return ""
    i += len(start)
    j = len(text)
    for e in ends:
        k = low.find(e.lower(), i)
        if k != -1:
            j = min(j, k)
    return text[i:j]


def _first_visible(ctx: Page | Frame, key: str, timeout_ms: int):
    """First candidate selector under SELECTORS[key] that is visible within the
    timeout. Raises ScraperError if none match."""
    deadline_each = max(1_000, timeout_ms // max(1, len(SELECTORS[key])))
    last_exc: Exception | None = None
    for sel in SELECTORS[key]:
        loc = ctx.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=deadline_each)
            return loc
        except PlaywrightTimeoutError as exc:
            last_exc = exc
    raise ScraperError(
        f"None of the {key!r} selectors matched a visible element: {SELECTORS[key]}"
    ) from last_exc


def _exists(ctx: Page | Frame, key: str, timeout_ms: int) -> bool:
    try:
        _first_visible(ctx, key, timeout_ms)
        return True
    except ScraperError:
        return False


def _slug(text: str) -> str:
    """'Mercedes-Benz of Northlake' -> 'mercedes-benz-of-northlake' for filenames."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "x"


def _ipacket_business_hours_ok(now: datetime | None = None) -> bool:
    """True if `now` (local time) falls Mon-Sat 8:00am-6:00pm."""
    now = now or datetime.now()
    if now.weekday() == 6:  # Sunday
        return False
    return IPACKET_BUSINESS_HOUR_START <= now.hour < IPACKET_BUSINESS_HOUR_END


def _ipacket_load_daily_count() -> dict[str, Any]:
    """Today's {'date', 'count'} for the /stickerpull endpoint, resetting to 0
    if the stored date isn't today."""
    today = datetime.now().date().isoformat()
    data: dict[str, Any] = {}
    if IPACKET_DAILY_COUNT_FILE.exists():
        try:
            data = json.loads(IPACKET_DAILY_COUNT_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    return data


def _ipacket_save_daily_count(data: dict[str, Any]) -> None:
    IPACKET_DAILY_COUNT_FILE.write_text(json.dumps(data), encoding="utf-8")


def _ipacket_check_and_increment_daily_count(enforce_limit: bool = True) -> int | None:
    """Bump today's /stickerpull count and return the new (1-based) total, or
    None if today's count is already at IPACKET_DAILY_LIMIT. With
    enforce_limit=False (interactive Flask pulls) the limit is not applied,
    but the pull is still counted so batch runs see it."""
    data = _ipacket_load_daily_count()
    if enforce_limit and data.get("count", 0) >= IPACKET_DAILY_LIMIT:
        return None
    data["count"] = data.get("count", 0) + 1
    _ipacket_save_daily_count(data)
    return data["count"]


# --------------------------------------------------------------------------- #
# Non-MB sticker rate limiters (see NON_MB_STICKER_* constants above). Used
# only by pull_sticker_endpoint() (tier 3, the live download) when the caller
# says the vehicle is non-MB — tiers 1 and 2 are never gated by any of this.
# crawl_ipacket_inventory() skips every non-MB VIN outright and never uses them.
#
# Two independent DAILY COUNTS (bypassed entirely by the Flask route):
#   * manual pulls  — NON_MB_STICKER_DAILY_LIMIT
#   * downloads     — NON_MB_STICKER_DOWNLOAD_DAILY_LIMIT
# and ONE shared "last pull at" TIMESTAMP that drives the 5-minute spacing check.
# The timestamp is recorded on every real non-MB tier-3 attempt, Flask
# included, so a Flask pull still makes a batch attempt seconds later wait out
# the remaining delay even though the Flask pull never counted toward a cap.
# --------------------------------------------------------------------------- #


def _non_mb_sticker_business_hours_ok(now: datetime | None = None) -> bool:
    """True if `now` (local time) falls Mon-Sat 11:00am-6:00pm."""
    now = now or datetime.now()
    if now.weekday() == 6:  # Sunday
        return False
    return (
        NON_MB_STICKER_BUSINESS_HOUR_START
        <= now.hour
        < NON_MB_STICKER_BUSINESS_HOUR_END
    )


def _non_mb_sticker_load_daily_count() -> dict[str, Any]:
    """Today's {'date', 'count'} for the manual-pull counter; `count` resets to
    0 when the stored date isn't today."""
    today = datetime.now().date().isoformat()
    data: dict[str, Any] = {}
    if NON_MB_STICKER_DAILY_COUNT_FILE.exists():
        try:
            data = json.loads(NON_MB_STICKER_DAILY_COUNT_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    return data


def _non_mb_sticker_save_daily_count(data: dict[str, Any]) -> None:
    NON_MB_STICKER_DAILY_COUNT_FILE.write_text(json.dumps(data), encoding="utf-8")


def _non_mb_sticker_daily_would_allow() -> bool:
    """PEEK: would a non-MB MANUAL-PULL be allowed right now? True only inside
    the Mon-Sat 11am-6pm window, with the 5-minute spacing since the last
    non-MB pull elapsed, and today's manual-pull count under
    NON_MB_STICKER_DAILY_LIMIT. No side effects — nothing is incremented or
    stamped."""
    return (
        _non_mb_sticker_business_hours_ok()
        and _non_mb_sticker_spacing_ok()
        and _non_mb_sticker_load_daily_count().get("count", 0) < NON_MB_STICKER_DAILY_LIMIT
    )


def _non_mb_sticker_daily_commit(vin: str) -> int:
    """COMMIT: bump today's manual-pull count for `vin` and log '[scraper]
    non-MB sticker manual pull X of {NON_MB_STICKER_DAILY_LIMIT} today — VIN
    {vin}'. Call only after _non_mb_sticker_daily_would_allow() (and the other
    counter's peek) passed. Count only — the spacing timestamp is stamped by
    _record_non_mb_sticker_pull_timestamp() on every real attempt, which
    includes Flask pulls that never commit a count."""
    data = _non_mb_sticker_load_daily_count()
    data["count"] = data.get("count", 0) + 1
    _non_mb_sticker_save_daily_count(data)
    print(
        f"[scraper] non-MB sticker manual pull {data['count']} of "
        f"{NON_MB_STICKER_DAILY_LIMIT} today — VIN {vin}"
    )
    return data["count"]


def _non_mb_sticker_check_and_increment_daily_count(vin: str) -> int | None:
    """Peek + commit for the manual-pull counter alone: the new (1-based) total,
    or None (no side effects) if _non_mb_sticker_daily_would_allow() says no.
    Kept for standalone use; the tier-3 gate peeks at BOTH counters first and
    only then commits both, so it never uses this."""
    if not _non_mb_sticker_daily_would_allow():
        return None
    return _non_mb_sticker_daily_commit(vin)


def _non_mb_sticker_download_load_count() -> dict[str, Any]:
    """Today's {'date', 'count'} for the DOWNLOAD counter; same reset behavior
    as _non_mb_sticker_load_daily_count(), separate file."""
    today = datetime.now().date().isoformat()
    data: dict[str, Any] = {}
    if NON_MB_STICKER_DOWNLOAD_COUNT_FILE.exists():
        try:
            data = json.loads(NON_MB_STICKER_DOWNLOAD_COUNT_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    return data


def _non_mb_sticker_download_save_count(data: dict[str, Any]) -> None:
    NON_MB_STICKER_DOWNLOAD_COUNT_FILE.write_text(json.dumps(data), encoding="utf-8")


def _non_mb_sticker_download_would_allow() -> bool:
    """PEEK: would a non-MB sticker DOWNLOAD be allowed right now? True only
    inside the Mon-Sat 11am-6pm window, with the 5-minute spacing since the
    last non-MB pull elapsed, and today's download count under
    NON_MB_STICKER_DOWNLOAD_DAILY_LIMIT. No side effects."""
    return (
        _non_mb_sticker_business_hours_ok()
        and _non_mb_sticker_spacing_ok()
        and _non_mb_sticker_download_load_count().get("count", 0)
        < NON_MB_STICKER_DOWNLOAD_DAILY_LIMIT
    )


def _non_mb_sticker_download_commit(vin: str) -> int:
    """COMMIT: bump today's download count for `vin` and log '[scraper] non-MB
    sticker download X of {NON_MB_STICKER_DOWNLOAD_DAILY_LIMIT} today — VIN
    {vin}'. Call only after both counters' peeks passed. Count only (see
    _non_mb_sticker_daily_commit() on the spacing timestamp)."""
    data = _non_mb_sticker_download_load_count()
    data["count"] = data.get("count", 0) + 1
    _non_mb_sticker_download_save_count(data)
    print(
        f"[scraper] non-MB sticker download {data['count']} of "
        f"{NON_MB_STICKER_DOWNLOAD_DAILY_LIMIT} today — VIN {vin}"
    )
    return data["count"]


def _non_mb_sticker_download_check_and_increment(vin: str) -> int | None:
    """Peek + commit for the download counter alone: the new (1-based) total, or
    None (no side effects) if _non_mb_sticker_download_would_allow() says no.
    Kept for standalone use; the tier-3 gate peeks at both counters first."""
    if not _non_mb_sticker_download_would_allow():
        return None
    return _non_mb_sticker_download_commit(vin)


def _non_mb_sticker_last_pull_at() -> datetime | None:
    """When the last real non-MB tier-3 pull was attempted (Flask or batch), or
    None if never / unreadable. Carries across midnight on purpose."""
    if not NON_MB_STICKER_LAST_PULL_FILE.exists():
        return None
    try:
        raw = json.loads(NON_MB_STICKER_LAST_PULL_FILE.read_text(encoding="utf-8"))
        return datetime.fromisoformat(raw["last_pull_at"])
    except (ValueError, OSError, KeyError, TypeError):
        return None


def _record_non_mb_sticker_pull_timestamp() -> None:
    """Stamp 'now' as the last non-MB tier-3 pull attempt. Called for every
    real attempt regardless of bypass_rate_limits — it is what makes a batch
    attempt wait out the spacing after an uncounted Flask pull."""
    NON_MB_STICKER_LAST_PULL_FILE.write_text(
        json.dumps({"last_pull_at": datetime.now().isoformat()}), encoding="utf-8"
    )


def _non_mb_sticker_spacing_ok() -> bool:
    """True if at least NON_MB_STICKER_MIN_DELAY_SECONDS have passed since the
    last recorded non-MB pull attempt. Refuses only; never sleeps."""
    last_at = _non_mb_sticker_last_pull_at()
    if last_at is None:
        return True
    return (datetime.now() - last_at).total_seconds() >= NON_MB_STICKER_MIN_DELAY_SECONDS


def _non_mb_sticker_gate_refusal() -> str | None:
    """PEEK at BOTH non-MB counters for a non-Flask non-MB tier-3 pull: a short
    reason string if either would_allow check fails, else None. Side-effect
    free — no count bumped, no timestamp stamped — so a refusal by one gate can
    never burn a slot on the other. Only when this returns None does the
    caller commit both counts (_non_mb_sticker_download_commit() and
    _non_mb_sticker_daily_commit()). The reason is worked out in the same
    order the checks run."""
    if _non_mb_sticker_download_would_allow() and _non_mb_sticker_daily_would_allow():
        return None
    if not _non_mb_sticker_business_hours_ok():
        return "outside the non-MB pull window (Mon-Sat 11am-6pm)"
    if not _non_mb_sticker_spacing_ok():
        return f"less than {NON_MB_STICKER_MIN_DELAY_SECONDS}s since the last non-MB pull"
    if not _non_mb_sticker_download_would_allow():
        return f"non-MB download limit reached ({NON_MB_STICKER_DOWNLOAD_DAILY_LIMIT}/day)"
    return f"non-MB manual-pull limit reached ({NON_MB_STICKER_DAILY_LIMIT}/day)"


# --------------------------------------------------------------------------- #
# Shared browser-session lifecycle
# --------------------------------------------------------------------------- #


_SESSION_THREAD_LOCK = threading.RLock()
_session_depth = 0


class _BrowserSession:
    """Playwright lifecycle + saved-session + debug-dump plumbing shared by the
    concrete scrapers. Subclasses set SESSION_FILENAME."""

    SESSION_FILENAME = "session.json"

    def __init__(
        self,
        *,
        headless: bool = True,
        slow_mo_ms: int = 0,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        use_saved_session: bool = True,
        keep_open: bool = False,
        viewport: dict[str, int] | None = None,
    ) -> None:
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.timeout_ms = timeout_ms
        self.use_saved_session = use_saved_session
        self.keep_open = keep_open
        self.session_path = Path(__file__).with_name(self.SESSION_FILENAME)
        self._viewport = viewport

        self._pw = None
        self._browser = None
        self._context = None
        self.page: Page | None = None

    def __enter__(self):
        # The lock file is per-process and released only by the OUTERMOST
        # session: the orchestrator keeps a ReconVision session open while
        # aggregate() opens ACV Max / AutoiPacket ones inside it, and an inner
        # exit must not free the lock under the outer one. The RLock also makes
        # a second thread in this process wait instead of sailing through the
        # PID-reentrant file lock.
        _SESSION_THREAD_LOCK.acquire()
        try:
            global _session_depth
            if _session_depth == 0:
                acquire_scraper_lock(SCRAPER_LOCK_PATH, wait_seconds=30)
            try:
                self.start()
            except Exception:
                if _session_depth == 0:
                    release_scraper_lock(SCRAPER_LOCK_PATH)
                raise
            _session_depth += 1
        except BaseException:
            _SESSION_THREAD_LOCK.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        global _session_depth
        try:
            try:
                if self.keep_open and not self.headless and self.page is not None:
                    print("[scraper] --keep-open set; leaving browser open. Ctrl-C to exit.")
                    try:
                        self.page.wait_for_timeout(10 * 60 * 1000)
                    except KeyboardInterrupt:
                        pass
                self.stop()
            finally:
                _session_depth -= 1
                if _session_depth == 0:
                    release_scraper_lock(SCRAPER_LOCK_PATH)
        finally:
            _SESSION_THREAD_LOCK.release()

    def start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless, slow_mo=self.slow_mo_ms
        )
        state = (
            str(self.session_path)
            if self.use_saved_session and self.session_path.exists()
            else None
        )
        ctx_kwargs: dict[str, Any] = {"storage_state": state}
        if self._viewport:
            ctx_kwargs["viewport"] = self._viewport
        self._context = self._browser.new_context(**ctx_kwargs)
        self._context.set_default_timeout(self.timeout_ms)
        self.page = self._context.new_page()

    def stop(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._pw = self._browser = self._context = self.page = None

    def _save_session(self) -> None:
        if self._context is not None:
            self._context.storage_state(path=str(self.session_path))

    def _dump_debug(self, tag: str, page: Page | None = None) -> None:
        pg = page or self.page
        if pg is None:
            return
        DEBUG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            pg.screenshot(path=str(DEBUG_DIR / f"{stamp}-{tag}.png"), full_page=True)
            (DEBUG_DIR / f"{stamp}-{tag}.html").write_text(
                pg.content(), encoding="utf-8"
            )
            print(f"[scraper] wrote debug artifacts to {DEBUG_DIR}/{stamp}-{tag}.*")
        except Exception as exc:  # debugging aid only, never fatal
            print(f"[scraper] could not write debug artifacts: {exc}")


# --------------------------------------------------------------------------- #
# AutoiPacket scraper
# --------------------------------------------------------------------------- #


class AutoiPacketScraper(_BrowserSession):
    """Log into AutoiPacket and pull window-sticker data by VIN.

    Usage:
        with AutoiPacketScraper(headless=True) as s:
            data = s.scrape("W1NKM4GB0PU017453")
    """

    SESSION_FILENAME = "aip_session.json"

    # -- auth ------------------------------------------------------------- #

    def is_logged_in(self) -> bool:
        """True if the current session reaches the Sticker Pull form."""
        assert self.page is not None
        self.page.goto(STICKER_PULL_URL, wait_until="domcontentloaded")
        self.page.wait_for_load_state("networkidle")
        if _exists(self.page, "dpapp_login_marker", timeout_ms=2_500):
            return False
        return _exists(self.page, "vin_input", timeout_ms=8_000)

    def login(self, *, force: bool = False) -> None:
        assert self.page is not None

        if not force and self.use_saved_session and self.is_logged_in():
            print("[scraper] reusing saved AutoiPacket session")
            return

        email = getattr(credentials, "AUTOIPACKET_EMAIL", "")
        password = getattr(credentials, "AUTOIPACKET_PASSWORD", "")
        if not email or "@" not in email or password in ("", "changeme", "your_password_here"):
            raise LoginError(
                "AUTOIPACKET_EMAIL / AUTOIPACKET_PASSWORD are not set in credentials.py"
            )

        print(f"[scraper] logging into {AUTH_URL}")
        self.page.goto(AUTH_URL, wait_until="domcontentloaded")
        try:
            _first_visible(self.page, "login_email", self.timeout_ms).fill(email)
            _first_visible(self.page, "login_password", self.timeout_ms).fill(password)
            with self.page.expect_navigation(wait_until="domcontentloaded"):
                _first_visible(self.page, "login_submit", self.timeout_ms).click()
        except ScraperError:
            self._dump_debug("login-form")
            raise

        self.page.wait_for_load_state("networkidle")
        if _exists(self.page, "login_password", timeout_ms=4_000) or not _exists(
            self.page, "logged_in_marker", timeout_ms=10_000
        ):
            self._dump_debug("login-failed")
            raise LoginError(
                "Login did not complete — login form still visible or no post-login "
                "marker appeared. Check credentials.py and the login_* selectors."
            )
        print("[scraper] auth.autoipacket.com login OK")

        self._establish_dealer_portal_session()
        self._save_session()
        print("[scraper] Dealer Portal session established, session saved")

    def _establish_dealer_portal_session(self) -> None:
        """Click through 'Dealer Portal' so dpapp.autoipacket.com gets its cookies."""
        assert self.page is not None
        if "auth.autoipacket.com" in self.page.url:
            self.page.goto(ACCOUNT_URL, wait_until="networkidle")
        try:
            with self.page.expect_navigation(wait_until="networkidle"):
                _first_visible(self.page, "dealer_portal_link", self.timeout_ms).click()
        except (ScraperError, PlaywrightTimeoutError):
            self._dump_debug("sso-handoff")
            raise LoginError("Could not follow the 'Dealer Portal' SSO link.")

        if "dpapp.autoipacket.com" not in self.page.url:
            self._dump_debug("sso-handoff")
            raise LoginError(
                f"SSO handoff did not land on dpapp.autoipacket.com (at {self.page.url})."
            )

    # -- sticker pull --------------------------------------------------- #

    def pull_sticker(
        self, vin: str, *, bypass_rate_limits: bool = False, non_mb: bool = False
    ) -> dict[str, Any] | None:
        """Get window-sticker data for a VIN via a three-tier strategy:

          Tier 1  rarity.db cache      — a stored 'complete' row, no browser.
          Tier 2  iPacket browse       — search dpapp.../packets for the VIN and
                                         scrape its packet 'Original MSRP' tab.
          Tier 3  stickerpull endpoint — the original /stickerpull flow. Logs a
                                         warning every time so endpoint hits can
                                         be monitored.

        The return always carries a `source` field naming the tier that answered.
        `bypass_rate_limits` / `non_mb` only matter for tier 3 (the live
        download) and are handed straight to pull_sticker_endpoint(); tiers 1
        and 2 are never rate-limited.
        """
        assert self.page is not None
        vin = vin.strip().upper()
        if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{11,17}", vin):
            raise ScraperError(f"{vin!r} does not look like a VIN")

        # -- Tier 1: rarity.db cache ---------------------------------- #
        try:
            from vin_importer import get_sticker_from_db

            cached = get_sticker_from_db(vin)
        except Exception:  # noqa: BLE001 - a broken cache must never block a pull
            cached = None
        if cached:
            print(f"[scraper] sticker tier 1 (rarity_db_cache) for {vin}")
            cached["source"] = "rarity_db_cache"
            return cached

        # -- Tier 2: iPacket inventory browse ----------------------- #
        try:
            data = self._sticker_via_browse(vin)
        except ScraperError as exc:
            data = None
            print(f"[scraper] sticker tier 2 (ipacket_browse) miss for {vin}: {exc}")
        if data:
            print(f"[scraper] sticker tier 2 (ipacket_browse) for {vin}")
            data["source"] = "ipacket_browse"
            return data

        # -- Tier 3: stickerpull endpoint fallback ------------------ #
        print(
            f"[scraper] WARNING: sticker tier 3 (ipacket_stickerpull) for {vin} "
            f"— tiers 1 and 2 did not answer; hitting the sticker-pull endpoint",
            file=sys.stderr,
        )
        print(f"[scraper] sticker tier 3 (ipacket_stickerpull) for {vin}")
        return self.pull_sticker_endpoint(
            vin, bypass_rate_limits=bypass_rate_limits, non_mb=non_mb
        )

    def pull_sticker_endpoint(
        self, vin: str, *, bypass_rate_limits: bool = False, non_mb: bool = False
    ) -> dict[str, Any] | None:
        """Direct /stickerpull flow: type the VIN into the form and scrape the
        result. This is tier 3 of pull_sticker(), and the path the bulk importer
        uses (it wants the endpoint, not the cache/browse tiers). Return carries
        source='ipacket_stickerpull'.

        Rate-limited (this endpoint only — the packets-browse tiers and
        crawl_ipacket_inventory() are unaffected):
          * only runs Mon-Sat 8:00am-6:00pm local time
          * capped at IPACKET_DAILY_LIMIT pulls/day (ipacket_daily_count.json)
          * a random 45-90s delay before each request
        Returns None (no browser activity, no delay) if either gate blocks the
        call. `bypass_rate_limits=True` (interactive Flask requests) skips the
        hours gate and the daily caps (the pull is still counted against the
        iPacket total); the random delay before the request still applies.

        `non_mb=True` (a non-Mercedes vehicle) adds the stricter non-MB gates
        here, on the live download only: the Mon-Sat 11am-6pm window, the 5-minute
        spacing, and the manual-pull and download daily caps — both caps must
        pass. All gates are checked read-only first and the counts are bumped
        only once every gate has passed, so one refusal never burns a slot on
        another. A refused non-MB pull returns {"error": ..., "rate_limited":
        True} instead of None so the caller can tell "held back by a limit"
        from "the pull failed" (the latter counts toward the retry cap). The
        spacing timestamp is stamped on every real non-MB attempt, Flask
        included.
        """
        assert self.page is not None
        vin = vin.strip().upper()

        def _refused(reason: str) -> dict[str, Any] | None:
            print(f"[scraper] sticker pull for {vin} skipped — {reason}")
            return {"error": f"AutoiPacket pull skipped: {reason}", "rate_limited": True} if non_mb else None

        if not bypass_rate_limits:
            if not _ipacket_business_hours_ok():
                return _refused("iPacket outside business hours")
            if non_mb:
                reason = _non_mb_sticker_gate_refusal()
                if reason:
                    return _refused(reason)
            if _ipacket_load_daily_count().get("count", 0) >= IPACKET_DAILY_LIMIT:
                return _refused(f"iPacket daily sticker limit reached ({IPACKET_DAILY_LIMIT})")
            # Every peek passed (both non-MB counters, and the iPacket cap
            # above) — only now commit the two non-MB counts together.
            if non_mb:
                _non_mb_sticker_download_commit(vin)
                _non_mb_sticker_daily_commit(vin)

        count = _ipacket_check_and_increment_daily_count(
            enforce_limit=not bypass_rate_limits
        )
        if count is None:
            return _refused(f"iPacket daily sticker limit reached ({IPACKET_DAILY_LIMIT})")

        if non_mb:
            _record_non_mb_sticker_pull_timestamp()

        time.sleep(random.uniform(*IPACKET_DELAY_RANGE_SECONDS))
        print(
            f"[scraper] iPacket sticker pull {count} of {IPACKET_DAILY_LIMIT} "
            f"today — stock #{vin}"
        )

        if not self.page.url.startswith(STICKER_PULL_URL):
            self.page.goto(STICKER_PULL_URL, wait_until="networkidle")

        def _submit_vin() -> None:
            vin_field = _first_visible(self.page, "vin_input", self.timeout_ms)
            vin_field.fill("")
            vin_field.fill(vin)
            _first_visible(self.page, "vin_submit", self.timeout_ms).click()

        data = self._render_and_parse_sticker(vin, _submit_vin)
        data["source"] = "ipacket_stickerpull"
        return data

    def _render_and_parse_sticker(self, vin: str, trigger) -> dict[str, Any]:
        """Run `trigger` (the action that makes the sticker render), resolve
        whichever render path appears (HTML iframe or PDF), read it, and parse it.

        Shared by the stickerpull flow (VIN form submit) and the packet-page flow
        (the 'Original MSRP / Options Info' tab) — both render the identical
        document-viewer.autoipacket.com/sticker content.
        """
        assert self.page is not None
        # The PDF download URL (with its per-pull JWT) — captured from the network
        # log. The listener must be armed before `trigger` fires.
        self._captured_pdf_url: str | None = None

        def _capture_pdf_url(request) -> None:
            url = request.url
            if STICKER_PDF_DOWNLOAD_HINT in url and "token=" in url and vin in url.upper():
                self._captured_pdf_url = url

        self.page.on("request", _capture_pdf_url)
        try:
            trigger()
            kind, target = self._await_sticker_render(vin)
        finally:
            self.page.remove_listener("request", _capture_pdf_url)

        if kind == "html":
            text = self._read_sticker_frame(target)
            sticker_url = target.url
            image_path = self._capture_sticker_screenshot_html(target, vin)
        else:  # "pdf"
            if not target:
                self._dump_debug(f"no-pdf-url-{vin}")
                raise StickerNotFoundError(
                    f"{vin}: the sticker rendered as a PDF but no "
                    f"'{STICKER_PDF_DOWNLOAD_HINT}...token=' URL was seen in the "
                    f"network log."
                )
            text, image_path = self._read_sticker_pdf(target, vin)
            sticker_url = target

        data = self._parse_sticker_text(text, vin)
        data["sticker_url"] = sticker_url
        data["render"] = kind
        data["sticker_image_path"] = image_path
        if data["total_msrp"] is None and not data["option_packages"]:
            self._dump_debug(f"empty-parse-{vin}")
        return data

    @staticmethod
    def _capture_sticker_screenshot_html(frame: Frame, vin: str) -> str | None:
        """PNG of the rendered document-viewer sticker <iframe>'s body, saved
        to sticker_cache/<vin>.png for vision_parser.parse_sticker_image().
        Screenshots just the iframe's body element (not the surrounding
        dpapp.autoipacket.com app chrome) — best-effort, None on failure."""
        path = STICKER_CACHE_DIR / f"{vin}.png"
        try:
            frame.locator("body").screenshot(path=str(path), type="png")
        except Exception as exc:  # noqa: BLE001 - best-effort only
            print(f"[scraper] sticker screenshot capture failed for {vin}: {exc}")
            return None
        return str(path)

    def sticker_from_packet(self, packet_url: str, vin: str) -> dict[str, Any]:
        """From a packet detail page (/packet/view/<id>), open the
        'Original MSRP / Options Info' tab and scrape the window sticker.
        Same return shape as pull_sticker() (minus `source`, set by the caller).
        """
        assert self.page is not None
        vin = vin.strip().upper()
        self.page.goto(packet_url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeoutError:
            pass
        self.page.wait_for_timeout(1_500)

        def _open_msrp_tab() -> None:
            btn = self.page.get_by_role(
                "button", name=re.compile(r"Original MSRP", re.IGNORECASE)
            ).first
            try:
                btn.wait_for(state="visible", timeout=self.timeout_ms)
            except PlaywrightTimeoutError as exc:
                self._dump_debug(f"packet-no-msrp-tab-{vin}")
                raise StickerNotFoundError(
                    f"{vin}: 'Original MSRP / Options Info' tab not found on "
                    f"{packet_url}."
                ) from exc
            btn.click()

        return self._render_and_parse_sticker(vin, _open_msrp_tab)

    def _sticker_via_browse(self, vin: str) -> dict[str, Any] | None:
        """Tier 2: search the packets inventory list for the VIN and, if it's
        listed, scrape its packet detail page. Returns None if the VIN is not on
        the list (so the caller can fall through to tier 3)."""
        assert self.page is not None
        self.page.goto(PACKETS_URL, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeoutError:
            pass

        try:
            box = self.page.locator("form.search-bar input#query, input#query").first
            box.wait_for(state="visible", timeout=self.timeout_ms)
            box.fill("")
            box.fill(vin)
            box.press("Enter")
        except PlaywrightTimeoutError:
            return None

        self.page.wait_for_timeout(2_500)
        try:
            self.page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            pass

        href = self.page.evaluate(
            """(vin) => {
                const cards = [...document.querySelectorAll('div.car-container')];
                for (const c of cards) {
                    const a = c.querySelector("a[href*='/packet/view/']");
                    if (a && (c.innerText || '').toUpperCase().includes(vin)) {
                        return a.getAttribute('href');
                    }
                }
                if (cards.length === 1) {
                    const a = cards[0].querySelector("a[href*='/packet/view/']");
                    return a ? a.getAttribute('href') : null;
                }
                return null;
            }""",
            vin,
        )
        if not href:
            return None
        if href.startswith("/"):
            href = "https://dpapp.autoipacket.com" + href
        return self.sticker_from_packet(href, vin)

    def _await_sticker_render(self, vin: str) -> tuple[str, Any]:
        """Race the two render paths after VIN submission.

        Returns ("html", Frame) if the document-viewer iframe appears (within 8s,
        or later if the PDF hasn't shown either), or ("pdf", download_url) if the
        div.module-pdf render appears instead. Raises StickerNotFoundError if
        neither shows within 15s total.
        """
        assert self.page is not None
        iframe_sel = f"iframe[src*='{STICKER_FRAME_HINT}']"

        # Phase 1 — prefer the MB HTML iframe if it shows quickly.
        try:
            handle = self.page.wait_for_selector(iframe_sel, timeout=8_000)
            return ("html", self._resolve_sticker_frame(handle, vin))
        except PlaywrightTimeoutError:
            pass

        # Phase 2 — wait for whichever path appears, up to 15s total.
        try:
            self.page.wait_for_selector(
                f"{iframe_sel}, {STICKER_PDF_RENDER_SELECTOR}", timeout=7_000
            )
        except PlaywrightTimeoutError as exc:
            self._dump_debug(f"no-sticker-{vin}")
            raise StickerNotFoundError(
                f"Neither the sticker iframe nor a PDF render appeared for {vin} "
                f"within 15s. Common causes: VIN not covered by AutoiPacket, or a "
                f"year/make/model restriction. See {DEBUG_DIR}/ for a snapshot."
            ) from exc

        if self.page.locator(iframe_sel).count():
            handle = self.page.query_selector(iframe_sel)
            return ("html", self._resolve_sticker_frame(handle, vin))

        # PDF render path. Give the download request a moment to fire if needed.
        for _ in range(10):
            if self._captured_pdf_url:
                break
            self.page.wait_for_timeout(500)
        return ("pdf", self._captured_pdf_url)

    def _resolve_sticker_frame(self, handle, vin: str) -> Frame:
        """Turn a document-viewer <iframe> element handle into its Frame and wait
        for the sticker body to carry content."""
        assert self.page is not None
        frame = handle.content_frame() if handle else None
        for _ in range(20):
            if frame and STICKER_FRAME_HINT in (frame.url or ""):
                break
            self.page.wait_for_timeout(500)
            frame = next(
                (f for f in self.page.frames if STICKER_FRAME_HINT in (f.url or "")),
                frame,
            )
        if frame is None or STICKER_FRAME_HINT not in (frame.url or ""):
            self._dump_debug(f"sticker-frame-detach-{vin}")
            raise StickerNotFoundError(
                f"Sticker iframe for {vin} never attached with a usable URL."
            )

        try:
            frame.get_by_text(SEC_TOTAL, exact=False).first.wait_for(
                timeout=self.timeout_ms
            )
        except PlaywrightTimeoutError as exc:
            self._dump_debug(f"sticker-frame-empty-{vin}")
            raise StickerNotFoundError(
                f"Sticker iframe for {vin} loaded but never showed '{SEC_TOTAL}'."
            ) from exc
        return frame

    def _read_sticker_pdf(self, pdf_url: str, vin: str) -> tuple[str, str | None]:
        """Download the sticker PDF with the authenticated session and return
        (extracted text, path to a page-1 PNG render for vision_parser — None
        if that render failed, which never blocks the text extraction it
        rides along with)."""
        assert self.page is not None
        resp = self.page.context.request.get(pdf_url)
        if not resp.ok:
            self._dump_debug(f"pdf-download-{vin}")
            raise StickerNotFoundError(
                f"{vin}: sticker PDF download returned HTTP {resp.status}."
            )
        pdf_bytes = resp.body()
        text = self._pdf_bytes_to_text(pdf_bytes)
        if not text.strip():
            self._dump_debug(f"pdf-empty-{vin}")
            raise StickerNotFoundError(
                f"{vin}: sticker PDF downloaded but no text could be extracted."
            )
        image_path = self._render_pdf_page_image(pdf_bytes, vin)
        return text, image_path

    @staticmethod
    def _render_pdf_page_image(pdf_bytes: bytes, vin: str) -> str | None:
        """PNG render of a PDF sticker's first page, saved to
        sticker_cache/<vin>.png for vision_parser.parse_sticker_image().
        Non-Mercedes stickers render as a downloaded PDF rather than an
        on-screen iframe (see _parse_sticker_text_nonmb), so there's nothing
        to page.screenshot() — this rasterizes the PDF itself instead.
        Best-effort: None on any failure (missing pdfplumber, a malformed
        PDF, ...), never raises."""
        path = STICKER_CACHE_DIR / f"{vin}.png"
        try:
            import pdfplumber  # type: ignore

            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                if not pdf.pages:
                    return None
                pdf.pages[0].to_image(resolution=150).original.save(path, format="PNG")
        except Exception as exc:  # noqa: BLE001 - best-effort only
            print(f"[scraper] sticker PDF-to-image render failed for {vin}: {exc}")
            return None
        return str(path)

    @staticmethod
    def _pdf_bytes_to_text(pdf_bytes: bytes) -> str:
        """Extract text from a window-sticker PDF. Prefers pdfplumber; falls back
        to pypdf (pure-Python) where pdfplumber's compiled deps are unavailable."""
        buf = io.BytesIO(pdf_bytes)
        try:
            import pdfplumber  # type: ignore

            with pdfplumber.open(buf) as pdf:
                return "\n".join((p.extract_text() or "") for p in pdf.pages)
        except ImportError:
            pass
        try:
            from pypdf import PdfReader  # type: ignore

            buf.seek(0)
            reader = PdfReader(buf)
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        except ImportError as exc:
            raise StickerNotFoundError(
                "Reading a non-Mercedes (PDF) sticker needs a PDF text extractor. "
                "Install one: `pip3 install pdfplumber` (or `pip3 install pypdf`)."
            ) from exc

    @staticmethod
    def _read_sticker_frame(frame: Frame) -> str:
        try:
            tab = frame.get_by_text("Text Only", exact=True).first
            if tab.count():
                tab.click(timeout=3_000)
                frame.wait_for_timeout(500)
        except PlaywrightTimeoutError:
            pass
        return frame.locator("body").inner_text()

    # -- parsing ------------------------------------------------------ #

    @staticmethod
    def _parse_sticker_text(text: str, vin: str) -> dict[str, Any]:
        """Dispatch to the Mercedes-Benz (HTML) or non-Mercedes (PDF) parser
        based on which label set the sticker text uses. Both return the same
        dict schema."""
        up = text.upper()
        if "STANDARD FEATURES" in up and "STANDARD OPTIONS" not in up:
            return AutoiPacketScraper._parse_sticker_text_nonmb(text, vin)
        return AutoiPacketScraper._parse_sticker_text_mb(text, vin)

    @staticmethod
    def _parse_sticker_text_mb(text: str, vin: str) -> dict[str, Any]:
        lines = [_clean(ln) for ln in text.splitlines()]
        lines = [ln for ln in lines if ln not in _CHROME_LINES]

        def section_index(name: str) -> int | None:
            for i, ln in enumerate(lines):
                if ln.upper() == name:
                    return i
            return None

        i_std = section_index(SEC_STANDARD)
        i_base = section_index(SEC_BASE)
        i_added = section_index(SEC_ADDED)
        i_details = section_index(SEC_PRICE_DETAILS)

        header_end = i_std if i_std is not None else (i_base or len(lines))
        header = lines[:header_end]

        year_make_model = header[0] if header else None
        exterior_color = interior_color = None
        total_msrp = None
        for j, ln in enumerate(header):
            up = ln.upper()
            nxt = header[j + 1] if j + 1 < len(header) else ""
            if up == "INTERIOR":
                interior_color = nxt or None
            elif up == "EXTERIOR":
                exterior_color = nxt or None
            elif up == SEC_TOTAL:
                total_msrp = _money(ln) or _money(nxt)

        def parse_options(block: list[str]) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            k = 0
            while k < len(block):
                m = _OPTION_LINE.match(block[k])
                if not m:
                    k += 1
                    continue
                price = None
                if k + 1 < len(block) and _MONEY_LINE.match(block[k + 1]):
                    price = _money(block[k + 1])
                    k += 1
                out.append(
                    {"code": m.group(1), "name": _clean(m.group(2)), "price": price}
                )
                k += 1
            return out

        standard_options: list[dict[str, Any]] = []
        if i_std is not None:
            end = i_base if i_base is not None else (i_added or len(lines))
            standard_options = [
                {"code": c["code"], "name": c["name"]}
                for c in parse_options(lines[i_std + 1 : end])
            ]

        base_price = None
        if i_base is not None:
            tail = lines[i_base : i_base + 3]
            for ln in tail:
                base_price = base_price or (
                    _money(ln) if _MONEY_LINE.match(ln) or "$" in ln else None
                )

        added_options_all: list[dict[str, Any]] = []
        if i_added is not None:
            end = i_details if i_details is not None else len(lines)
            added_options_all = parse_options(lines[i_added + 1 : end])
        option_packages = [o for o in added_options_all if o["price"] is not None]

        freight = None
        if i_details is not None:
            details = lines[i_details + 1 :]
            for j, ln in enumerate(details):
                up = ln.upper()
                nxt = details[j + 1] if j + 1 < len(details) else ""
                if up.startswith("FREIGHT") or up.startswith("DESTINATION"):
                    freight = _money(ln) or _money(nxt)
                elif up == SEC_TOTAL:
                    total_msrp = _money(ln) or _money(nxt) or total_msrp

        return {
            "source": "autoipacket",
            "source_url": STICKER_PULL_URL,
            "sticker_url": None,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "vin": vin,
            "year_make_model": year_make_model,
            "exterior_color": exterior_color,
            "interior_color": interior_color,
            "base_price": base_price,
            "freight": freight,
            "total_msrp": total_msrp,
            "option_packages": option_packages,
            "added_options_all": added_options_all,
            "standard_options": standard_options,
            "raw_text": text,
        }

    @staticmethod
    def _parse_sticker_text_nonmb(text: str, vin: str) -> dict[str, Any]:
        """Parser for non-Mercedes window stickers (PDF layout): 'STANDARD
        FEATURES:' / 'ADDED FEATURES:' sections, 'Manufacturer's Suggested Retail
        Price' base, 'Total Price' total, inline 'EXTERIOR COLOR: x' /
        'INTERIOR/SEAT COLOR: x'. Returns the same schema as the MB parser."""
        lines = [_clean(ln) for ln in text.splitlines()]
        lines = [ln for ln in lines if ln and ln not in _CHROME_LINES]
        joined = "\n".join(lines)

        # --- year / make / model (clean header in the first several lines) ---
        year_make_model = next(
            (ln for ln in lines[:14] if re.match(r"^(?:19|20)\d{2}\b", ln)), None
        )
        if year_make_model:
            dt = re.search(
                re.escape(year_make_model) + rf"\n\s*({_NONMB_DRIVETRAIN})\b",
                joined, re.IGNORECASE,
            )
            if dt and dt.group(1).upper() not in year_make_model.upper():
                year_make_model = f"{year_make_model} {dt.group(1).upper()}"
            mk = re.search(
                r"\b([A-Z][a-zA-Z]+)\s+Motors?\s+(?:America|Company|Corp\w*)\b", text
            )
            if mk and mk.group(1).upper() not in year_make_model.upper():
                year_make_model = re.sub(
                    r"^((?:19|20)\d{2})\b",
                    r"\1 " + mk.group(1).upper(),
                    year_make_model,
                    count=1,
                )

        # --- inline colors ---
        # Label/slash spacing varies by PDF text extraction ("INTERIOR/SEAT
        # COLOR:" vs "INTERIOR / SEAT COLOR :"), so both are tolerant of
        # whitespace around the slash and the label itself.
        ext = re.search(r"EXTERIOR\s*COLOR\s*:\s*(.+)", joined, re.IGNORECASE)
        intr = re.search(
            r"INTERIOR(?:\s*/\s*SEAT)?\s*COLOR\s*:\s*(.+)", joined, re.IGNORECASE
        )
        exterior_color = _clean_nonmb_color(ext.group(1)) if ext else None
        interior_color = _clean_nonmb_color(intr.group(1)) if intr else None

        # --- prices ---
        bm = re.search(
            r"Manufacturer'?s Suggested Retail Price\s*:?\s*\$([\d,]+(?:\.\d{2})?)",
            joined, re.IGNORECASE,
        )
        base_price = _money(bm.group(1)) if bm else None
        tm = re.search(
            r"Total Price\s*:?\s*\$([\d,]+(?:\.\d{2})?)", joined, re.IGNORECASE
        )
        total_msrp = _money(tm.group(1)) if tm else None
        fm = re.search(
            r"(?:Inland Freight(?:\s*(?:&|and)\s*Handling)?|Freight(?:\s*(?:&|and)\s*"
            r"Handling)?|Destination(?: Charge)?)\s*:?\s*\$([\d,]+(?:\.\d{2})?)",
            joined, re.IGNORECASE,
        )
        freight = _money(fm.group(1)) if fm else None

        def _slice(start_kw: str, stop_kws: tuple[str, ...]) -> list[str]:
            start = next(
                (i + 1 for i, ln in enumerate(lines) if start_kw in ln.upper()), None
            )
            if start is None:
                return []
            block: list[str] = []
            for ln in lines[start:]:
                if any(s in ln.upper() for s in stop_kws):
                    break
                block.append(ln)
            return block

        # --- ADDED FEATURES ---
        # Only a line carrying its own price is ever a package. ALL CAPS
        # category headers (a non-MB sticker's way of grouping standard
        # features, not add-on packages) are pulled out into section_headers
        # and never treated as a package or attributed as a package's
        # sub-item, regardless of which section they appear inside.
        section_headers: list[str] = []
        added_options_all: list[dict[str, Any]] = []
        for ln in _slice(
            "ADDED FEATURES",
            ("INLAND FREIGHT", "TOTAL PRICE", "FUEL ECONOMY", "PARTS CONTENT",
             "MANUFACTURER'S SUGGESTED"),
        ):
            if _is_nonmb_section_header(ln):
                section_headers.append(ln)
                continue
            m = _NONMB_ADDED_LINE.match(ln)
            if m:
                added_options_all.append(
                    {
                        "code": None,
                        "name": _clean(m.group(1)).lstrip("*").strip(),
                        "price": _money(m.group(2)),
                    }
                )
            elif not ln.startswith("*") and added_options_all:
                # continuation description for the preceding priced item
                added_options_all.append(
                    {"code": None, "name": _clean(ln), "price": None}
                )
        option_packages = [o for o in added_options_all if o["price"] is not None]

        # --- STANDARD FEATURES ---
        standard_options: list[dict[str, Any]] = []
        for ln in _slice(
            "STANDARD FEATURES",
            ("MANUFACTURER'S SUGGESTED", "ADDED FEATURES", "TOTAL PRICE"),
        ):
            if _is_nonmb_section_header(ln):
                section_headers.append(ln)
                continue
            if re.search(r"\b(INCLUDED|STD)\b", ln, re.IGNORECASE):
                name = re.sub(
                    r"\s*\b(INCLUDED|STD|STANDARD)\b\s*$", "", ln, flags=re.IGNORECASE
                ).strip()
                if name:
                    standard_options.append({"code": None, "name": name})

        return {
            "source": "autoipacket",
            "source_url": STICKER_PULL_URL,
            "sticker_url": None,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "vin": vin,
            "year_make_model": year_make_model,
            "exterior_color": exterior_color,
            "interior_color": interior_color,
            "base_price": base_price,
            "freight": freight,
            "total_msrp": total_msrp,
            "option_packages": option_packages,
            "added_options_all": added_options_all,
            "standard_options": standard_options,
            "section_headers": section_headers,
            "raw_text": text,
        }

    # -- convenience ------------------------------------------------- #

    def scrape(self, vin: str) -> dict[str, Any]:
        self.login()
        return self.pull_sticker(vin)


# --------------------------------------------------------------------------- #
# iPacket inventory browse — build rarity.db from the packets list, not the
# sticker-pull endpoint.
# --------------------------------------------------------------------------- #

# One object per vehicle card on dpapp.autoipacket.com/packets.
_PACKET_CARD_JS = r"""
() => [...document.querySelectorAll('div.car-container')].map(card => {
  const a = card.querySelector("a[href*='/packet/view/']");
  const txt = (sel) => { const e = card.querySelector(sel); return e ? (e.innerText || '').replace(/\s+/g, ' ').trim() : null; };
  const primary = {}, secondary = {};
  card.querySelectorAll('.car-center-primary-detail').forEach(d => {
    const k = (d.querySelector('.car-center-primary-detail-label')?.innerText || '').replace(/\s+/g, ' ').trim();
    const v = (d.querySelector('.car-center-primary-detail-info')?.innerText || '').trim();
    if (k) primary[k] = v;
  });
  card.querySelectorAll('.car-center-secondary-detail').forEach(d => {
    const k = (d.querySelector('.car-center-secondary-detail-label')?.innerText || '').trim();
    const v = (d.querySelector('.car-center-secondary-detail-info')?.innerText || '').trim();
    if (k) secondary[k] = v;
  });
  return {
    packet_id: card.id || null,
    packet_url: a ? a.getAttribute('href') : null,
    year_make_model: txt('#vehicle-title') || txt('.car-center-ymm h1'),
    cpo_text: txt('.cpo-highlight'),
    vin: primary['VIN'] || null,
    stock_number: primary['Stock #'] || null,
    mileage: primary['Miles'] || null,
    location: primary['Location'] || null,
    trim: secondary['Trim'] || null,
    exterior_color: secondary['Exterior'] || null,
    interior_color: secondary['Interior'] || null,
    drivetrain: secondary['Drivetrain'] || null,
    inventory_type: secondary['Inventory Type'] || null,
    price: txt('.car-right-section-text-price'),
  };
}).filter(c => c.packet_url)
"""


def _num_or_none(text: Any) -> float | None:
    if text is None:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", str(text))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _ipacket_card_context(card: dict[str, Any]) -> dict[str, Any]:
    """Turn a collected packet card into the vin_importer context dict
    (year/make/model/trim/exterior_color/mileage/certified/price)."""
    ymm = (card.get("year_make_model") or "").strip()
    year = make = model = None
    m = re.match(r"((?:19|20)\d{2})\s+(.*)$", ymm)
    if m:
        year = int(m.group(1))
        rest = m.group(2).split()
        if rest and rest[0].lower() in ("land", "alfa", "aston", "rolls") and len(rest) > 1:
            make, rest = f"{rest[0]} {rest[1]}", rest[2:]
        elif rest:
            make, rest = rest[0], rest[1:]
        model = rest[0] if rest else None
    certified = 1 if re.search(r"certified", card.get("cpo_text") or "", re.IGNORECASE) else 0
    return {
        "year": year,
        "make": make,
        "model": model,
        "trim": (card.get("trim") or "").strip() or None,
        "exterior_color": card.get("exterior_color"),
        "mileage": _num_or_none(card.get("mileage")),
        "certified": certified,
        "price": _num_or_none(card.get("price")),
    }


def crawl_ipacket_inventory(
    *,
    headless: bool = True,
    fresh_login: bool = False,
    delay_seconds: int = 45,
    max_pages: int | None = None,
    limit: int | None = None,
    location_filter: str | None = None,
) -> int:
    """Build rarity.db by browsing the iPacket packets inventory list and each
    vehicle's packet detail page — NOT the sticker-pull endpoint.

    Phase 1 pages through the whole list collecting every /packet/view link and
    its card metadata (this page shows inventory across all stores this login can
    see). Phase 2 visits each: VINs already 'complete' in rarity.db are skipped
    entirely; the rest have their 'Original MSRP / Options Info' tab scraped and
    stored with source 'ipacket_inventory_browse' — in both rarity.db (the
    equipment-rarity corpus) and vehicle_cache.db (the ad-writer's per-vehicle
    cache, so a later ad write skips AutoiPacket entirely for this VIN).
    `delay_seconds` between vehicles; a single bad vehicle is marked 'failed'
    and never stops the run.

    Non-Mercedes-Benz VINs (see _is_mercedes_vin()) are skipped outright —
    non-MB sticker scraping is disabled pending reliable parsing for it (see
    the non_mb_sticker rate limiter below, built but not yet wired in).

    `location_filter`, if given, keeps only cards whose "Location" field matches
    it (case-insensitive substring), e.g. location_filter="Mercedes-Benz of
    Durham" restricts the crawl to that store's inventory.
    """
    from vin_importer import capture_sticker, is_captured, mark_failed
    from vehicle_cache import save_window_sticker

    loc_needle = (location_filter or "").strip().lower()
    collected: list[dict[str, Any]] = []
    skipped_other_location = 0
    seen: set[str] = set()

    with AutoiPacketScraper(
        headless=headless, use_saved_session=not fresh_login
    ) as s:
        s.login(force=fresh_login)
        page = s.page

        # -- Phase 1: collect every packet link across all pages ------ #
        page.goto(PACKETS_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=25_000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_selector("div.car-container", timeout=30_000)
        page.wait_for_timeout(1_500)

        last_page = 1
        try:
            nums = page.eval_on_selector_all(
                "div.paginator .paginator-control[id]",
                "els => els.map(e => parseInt(e.id, 10)).filter(n => !Number.isNaN(n))",
            )
            if nums:
                last_page = max(nums)
        except Exception:  # noqa: BLE001
            pass
        if max_pages:
            last_page = min(last_page, max_pages)
        print(f"[ipacket] inventory list spans {last_page} page(s)")

        page_no = 1
        while True:
            for c in page.evaluate(_PACKET_CARD_JS) or []:
                url = c.get("packet_url") or ""
                if url.startswith("/"):
                    url = "https://dpapp.autoipacket.com" + url
                c["packet_url"] = url
                vin = (c.get("vin") or "").strip().upper()
                c["vin"] = vin
                key = vin or url
                if not key or key in seen:
                    continue
                seen.add(key)
                if loc_needle and loc_needle not in (c.get("location") or "").lower():
                    skipped_other_location += 1
                    continue
                collected.append(c)
            print(
                f"[ipacket] page {page_no}/{last_page}: {len(collected)} vehicle(s) collected"
                + (f" ({skipped_other_location} skipped, other location)" if loc_needle else "")
            )

            if page_no >= last_page:
                break
            # The page renders a paginator both above and below the results, so
            # a bare `div.paginator-active-page` locator matches twice
            # (Playwright strict mode rejects a multi-match .inner_text()) —
            # always scope reads to .first. Within one paginator the button
            # order is « ... » — .last is the » (next) button.
            nxt = page.locator(
                "div.paginator .paginator-control.paginator-button"
            ).last
            if not nxt.count():
                break
            active_el = page.locator("div.paginator-active-page").first
            before = active_el.inner_text() if active_el.count() else ""
            try:
                nxt.click(timeout=8_000)
            except Exception:  # noqa: BLE001
                break
            advanced = False
            for _ in range(40):
                page.wait_for_timeout(500)
                active_el = page.locator("div.paginator-active-page").first
                now = active_el.inner_text() if active_el.count() else ""
                if now and now != before:
                    advanced = True
                    break
            if not advanced:
                break
            try:
                page.wait_for_selector("div.car-container", timeout=20_000)
            except PlaywrightTimeoutError:
                break
            page.wait_for_timeout(800)
            page_no += 1

        if limit is not None:
            collected = collected[:limit]
        total = len(collected)
        print(f"[ipacket] {total} vehicle(s) to process; visiting detail pages")

        # -- Phase 2: visit each detail page slowly ------------------- #
        counts = {"complete": 0, "skipped": 0, "failed": 0}
        for i, c in enumerate(collected, 1):
            vin = c.get("vin") or ""
            ymm = c.get("year_make_model") or "?"
            stock = c.get("stock_number") or "?"

            if not vin:
                counts["failed"] += 1
                print(
                    f"iPacket browse: {i} of {total} — {ymm} {stock} — "
                    f"failed (no VIN on card)",
                    file=sys.stderr,
                )
                continue

            if is_captured(vin):
                counts["skipped"] += 1
                print(
                    f"iPacket browse: {i} of {total} — {ymm} {stock} — "
                    f"skipped (already in DB)"
                )
                continue

            if not _is_mercedes_vin(vin):
                counts["skipped"] += 1
                print(
                    f"[scraper] skipping non-MB VIN {vin} — non-MB sticker "
                    f"scraping disabled"
                )
                continue

            ctx = _ipacket_card_context(c)
            try:
                data = s.sticker_from_packet(c["packet_url"], vin)
                captured = capture_sticker(
                    vin, data, context=ctx, source="ipacket_inventory_browse"
                )
                # Also warm vehicle_cache.db — but only on a genuine capture.
                # capture_sticker() returns False for unusable data (no
                # total_msrp and no option_packages); caching that would
                # poison vehicle_cache.db forever, since window stickers never
                # expire there.
                if captured:
                    save_window_sticker(
                        vin,
                        c.get("stock_number"),
                        c.get("year_make_model"),
                        data,
                        "ipacket_inventory_browse",
                    )
                counts["complete"] += 1
                print(
                    f"iPacket browse: {i} of {total} — {ymm} {stock} — complete"
                )
            except Exception as exc:  # noqa: BLE001 - never crash the run for one car
                try:
                    mark_failed(vin, exc, context=ctx)
                except Exception:  # noqa: BLE001
                    pass
                counts["failed"] += 1
                print(
                    f"iPacket browse: {i} of {total} — {ymm} {stock} — failed: {exc}",
                    file=sys.stderr,
                )

            time.sleep(delay_seconds)

    print()
    print("=" * 50)
    print("IPACKET INVENTORY BROWSE SUMMARY")
    print("=" * 50)
    if loc_needle:
        print(f"  Location filter:          {location_filter}")
        print(f"  Skipped (other location): {skipped_other_location}")
    print(f"  Total visited:            {counts['complete'] + counts['failed']}")
    print(f"  Complete:                 {counts['complete']}")
    print(f"  Already in DB (skipped):  {counts['skipped']}")
    print(f"  Failed:                   {counts['failed']}")
    print(f"  Databases:                rarity.db, vehicle_cache.db")
    return 0


# --------------------------------------------------------------------------- #
# OEM window-sticker links found on a Carfax report (non-Mercedes trade-ins
# whose AutoiPacket pull failed). Detection lives in ACVMaxScraper._parse_carfax
# below; _parse_oem_sticker() is the fetch+parse fallback aggregator.py calls.
# --------------------------------------------------------------------------- #

_STICKER_LINK_TEXT_RE = re.compile(
    r"window\s*sticker|monroney\s*label|original\s*sticker|build\s*sheet",
    re.IGNORECASE,
)
_STICKER_LINK_URL_RE = re.compile(
    r"windowsticker|monroney|buildsheet|sticker", re.IGNORECASE
)
# Known OEM sticker-hosting domains, keyed loosely to the make family they serve.
_STICKER_KNOWN_DOMAINS_RE = re.compile(
    r"windowsticker\.forddirect\.com"          # Ford / Lincoln
    r"|gmauthority\.com|cpr\.gm\.com"            # GM: Chevy / Buick / GMC / Cadillac
    r"|mopar\.com|media\.vw\.com"                # Chrysler / Dodge / Jeep / Ram
    r"|toyota\.com/configurator/api/lexicon/models/sticker"  # Toyota / Lexus
    r"|monroneylabell\.honda\.com",              # Honda / Acura
    re.IGNORECASE,
)


def _find_window_sticker_link(html: str) -> tuple[str | None, str | None]:
    """Scan raw Carfax report HTML for a window-sticker / Monroney / build-sheet
    link or button. Returns (url, hit_kind) or (None, None). Hyundai/Kia are
    intentionally not matched here — they're already handled via the AutoiPacket
    PDF path."""
    if not html:
        return None, None

    for tag in ("a", "button"):
        attr = "href" if tag == "a" else r"data-(?:href|url)"
        for m in re.finditer(
            rf'<{tag}\b[^>]*\b{attr}\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</{tag}>',
            html,
            re.IGNORECASE | re.DOTALL,
        ):
            href = m.group(1).strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                continue
            inner_text = re.sub(r"<[^>]+>", " ", m.group(2))
            if _STICKER_KNOWN_DOMAINS_RE.search(href):
                return href, "known_domain"
            if _STICKER_LINK_URL_RE.search(href):
                return href, "url_pattern"
            if _STICKER_LINK_TEXT_RE.search(inner_text):
                return href, "link_text"
    return None, None


def _html_to_text(html: str) -> str:
    """Crude but dependency-free HTML -> text, good enough for pulling dollar
    figures and option descriptions out of an OEM sticker page."""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&")
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n\n", html)
    return html.strip()


# Same "description ... $amount" shape used for non-MB AutoiPacket stickers.
# Matches a 'Description ... $amount' / 'Description ... amount' /
# 'Description ... NO CHARGE' line. PDF text extraction for these stickers does
# not preserve layout, so option lines don't reliably carry a leading '$' — a
# bare decimal ("1,140.00") or a "NO CHARGE" / "-  50.00" (removal credit) tail
# is just as common.
_OEM_PRICE_LINE_RE = re.compile(
    r"^\*?\s*(.+?)\s+(?:\$?\s*(-?\s*[\d,]+(?:\.\d{2})?)|(NO\s+CHARGE))\s*$",
    re.IGNORECASE,
)
_OEM_TOTAL_KEYWORDS_RE = re.compile(
    r"\b(total|msrp|price|destination|freight|delivery|invoice)\b",
    re.IGNORECASE,
)


def _scan_oem_priced_lines(text: str, *, max_desc_len: int = 80) -> list[dict[str, Any]]:
    """Generic option-line scan: one entry per 'Description ... amount' line,
    skipping price-total rows and lines with no real description."""
    out: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _OEM_PRICE_LINE_RE.match(line)
        if not m:
            continue
        desc = m.group(1).strip()
        if (
            not desc
            or len(desc) > max_desc_len
            or _OEM_TOTAL_KEYWORDS_RE.search(desc)
            or not re.search(r"[A-Za-z]{3,}", desc)  # description must say something
        ):
            continue
        if m.group(3):  # "NO CHARGE"
            price = 0.0
        else:
            amt = re.sub(r"\s+", "", m.group(2) or "")
            try:
                price = float(amt.replace(",", ""))
            except ValueError:
                continue
        out.append({"code": None, "name": desc, "price": price})
    return out


def _find_oem_amount(text: str, *labels: str) -> float | None:
    # Require a real-looking amount (>= 3 digits) so a label that happens to sit
    # near unrelated boilerplate ("...THE STANDARD VEHICLE PRICE SHOWN\nCREW CAB
    # SHORT BOX 4WD") can't match a stray single digit like the "4" in "4WD".
    for label in labels:
        m = re.search(
            re.escape(label) + r"\D{0,25}\$?\s?(\d[\d,]{2,}(?:\.\d{2})?|\d+\.\d{2})",
            text,
            re.IGNORECASE,
        )
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _finalize_oem_totals(data: dict[str, Any], text: str) -> dict[str, Any]:
    """If the grand total wasn't found directly (its label and its dollar figure
    can be pages apart in these PDFs — the shipping/dealer address block usually
    sits between them), reconstruct it as base + freight + total options, using
    whichever of those labeled subtotals are present. Never fabricates a number
    from thin air — only combines other labeled figures actually found."""
    if data.get("total_msrp") is not None:
        return data
    total_options = _find_oem_amount(
        text, "TOTAL OPTIONS/OTHER", "TOTAL OPTIONS", "OPTIONS TOTAL", "TOTAL OPTION"
    )
    if data.get("base_price") is not None and total_options is not None:
        data["total_msrp"] = round(
            data["base_price"] + (data.get("freight") or 0) + total_options, 2
        )
    return data


def _parse_ford_sticker_text(text: str) -> dict[str, Any]:
    # The option lines live in one clean block; scanning the whole document also
    # picks up fuel-economy, safety-rating, and dealer-address noise that happens
    # to end in a number too.
    options_region = (
        _slice_between(text, "OPTIONAL EQUIPMENT/OTHER", ("PRICE INFORMATION",))
        or _slice_between(text, "INCLUDED ON THIS VEHICLE", ("PRICE INFORMATION",))
        or text
    )
    data = {
        "base_price": _find_oem_amount(text, "BASE VEHICLE PRICE", "BASE PRICE"),
        "freight": _find_oem_amount(
            text, "DESTINATION AND DELIVERY", "DESTINATION & DELIVERY",
            "DESTINATION CHARGE", "DELIVERY",
        ),
        # the grand total's label and figure are often pages apart here (a
        # dealer/shipping address block sits between them) — _finalize_oem_totals
        # reconstructs it from base + freight + total options when this misses.
        "total_msrp": _find_oem_amount(text, "TOTAL VEHICLE PRICE", "TOTAL PRICE"),
        "option_packages": _scan_oem_priced_lines(options_region),
        "standard_options": [],
    }
    return _finalize_oem_totals(data, text)


def _parse_gm_sticker_text(text: str) -> dict[str, Any]:
    options_region = (
        _slice_between(
            text, "OPTIONS INSTALLED BY THE MANUFACTURER", ("TOTAL OPTIONS",)
        )
        or _slice_between(text, "OPTIONS & PRICING", ("TOTAL OPTIONS",))
        or text
    )
    data = {
        "base_price": _find_oem_amount(
            text, "STANDARD VEHICLE PRICE", "BASE PRICE", "BASE MSRP"
        ),
        "freight": _find_oem_amount(
            text, "DESTINATION FREIGHT CHARGE", "DESTINATION CHARGE", "FREIGHT"
        ),
        "total_msrp": _find_oem_amount(
            text, "TOTAL VEHICLE PRICE", "TOTAL MSRP", "TOTAL PRICE"
        ),
        "option_packages": _scan_oem_priced_lines(options_region),
        "standard_options": [],
    }
    return _finalize_oem_totals(data, text)


def _parse_generic_oem_sticker_text(text: str) -> dict[str, Any]:
    data = {
        "base_price": _find_oem_amount(text, "BASE PRICE", "BASE MSRP", "BASE VEHICLE PRICE"),
        "freight": _find_oem_amount(text, "DESTINATION", "FREIGHT", "DELIVERY"),
        "total_msrp": _find_oem_amount(
            text,
            "TOTAL MSRP",
            "TOTAL PRICE",
            "TOTAL VEHICLE PRICE",
            "MANUFACTURER'S SUGGESTED RETAIL PRICE",
        ),
        "option_packages": _scan_oem_priced_lines(text),
        "standard_options": [],
    }
    return _finalize_oem_totals(data, text)


_STICKER_FETCH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _parse_oem_sticker(url: str, vin: str, make: str | None = None) -> dict[str, Any]:
    """Fetch and parse a non-AutoiPacket OEM window sticker found via a Carfax
    link (Ford PDF, GM HTML, or an unrecognized format handled generically).

    Returns the same shape as AutoiPacketScraper.pull_sticker() (base_price,
    freight, total_msrp, option_packages, standard_options, raw_text, render,
    sticker_url, source='carfax_sticker_link'). Raises StickerNotFoundError if
    the URL can't be fetched or nothing usable can be parsed out of it.
    """
    import ssl
    import urllib.error
    import urllib.request

    # macOS python.org builds don't populate OpenSSL's default trust store, so a
    # plain urlopen() fails HTTPS verification here. Same fallback chain as
    # adwriter._send_gmail: truststore (macOS Keychain) -> certifi -> default.
    try:
        import truststore

        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        try:
            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001
            ctx = ssl.create_default_context()

    req = urllib.request.Request(url, headers={"User-Agent": _STICKER_FETCH_UA})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            content_type = resp.headers.get("Content-Type", "") or ""
            body = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise StickerNotFoundError(
            f"{vin}: could not fetch OEM sticker at {url}: {exc}"
        ) from exc

    is_pdf = "pdf" in content_type.lower() or url.lower().split("?")[0].endswith(".pdf")
    if is_pdf:
        text = AutoiPacketScraper._pdf_bytes_to_text(body)
        render = "pdf"
    else:
        text = _html_to_text(body.decode("utf-8", errors="replace"))
        render = "html"

    if not text.strip():
        raise StickerNotFoundError(f"{vin}: OEM sticker at {url} had no extractable text.")

    make_low = (make or "").lower()
    if "ford" in make_low or "lincoln" in make_low:
        data = _parse_ford_sticker_text(text)
    elif any(m in make_low for m in ("chevrolet", "chevy", "buick", "gmc", "cadillac")):
        data = _parse_gm_sticker_text(text)
    else:
        data = _parse_generic_oem_sticker_text(text)

    if data["total_msrp"] is None and not data["option_packages"]:
        raise StickerNotFoundError(
            f"{vin}: OEM sticker at {url} fetched but no price or options parsed "
            f"out of it."
        )

    data.update(
        vin=vin,
        added_options_all=[],
        raw_text=text,
        render=render,
        sticker_url=url,
        source="carfax_sticker_link",
    )
    return data


# --------------------------------------------------------------------------- #
# ACV MAX scraper
# --------------------------------------------------------------------------- #


_PP_DIRECTION_RE = re.compile(r"^(above|below|at)\s+(.+?)\s*$", re.IGNORECASE)
_PP_CURRENT_RE = re.compile(r"^current price:\s*\$?([\d,]+(?:\.\d{2})?)", re.IGNORECASE)
_PP_BENCHMARK_RE = re.compile(r"^\$([\d,]+(?:\.\d{2})?)$")

# Market velocity widgets ("Define Competitive Set", the market days-supply
# chart, "Price 'In the Game'") — unlike the proof points section above, none
# of these follow a consistent "label line, value line" layout, so each is
# pulled straight out of the frame's full body text with its own scoped
# regex instead of the lines-list walk _parse_proof_points() uses. Confirmed
# against a live debug dump (scraper_debug/market_velocity_debug.txt,
# captured from stock P71019) rather than guessed from a screenshot:
#
#   Define Competitive Set
#   Matching
#   20
#   1071
#   Overall
#   ...
#   Avg Mileage:19,655 Market Days Supply: Overall62
#   Matching38
#   $54,987(102%)
#   low
#   ...
#   Rank
#   Internet Price
#   Unit Cost
#   of 20
#   Potential Gross Profit
#
# "Matching"/"Overall" each appear twice, in different word orders: the
# Competitive Set widget is "Matching" <newline> 20 <newline> 1071
# <newline> "Overall" (label before its value, value before the OTHER
# label), while the market days-supply chart is "Overall62"/"Matching38"
# (label immediately before its own value) — _COMPETITIVE_SET_RE and
# _MARKET_DAYS_SUPPLY_SECTION_RE are each scoped to their own section header so
# they can't cross-match the other widget's numbers.
#
# The "Price 'In the Game'" rank widget draws its rank NUMERATOR (e.g. the
# "16" in "16 of 20") as a graphic, not text — it never appears anywhere in
# frame.locator("body").inner_text() output, live-confirmed above, so
# market_rank cannot be scraped this way and is always None. Only the
# denominator ("of 20") is real text, giving market_rank_of.
_COMPETITIVE_SET_RE = re.compile(
    r"Define\s*Competitive\s*Set\D*?Matching\D*?([\d,]+)\D*?([\d,]+)\D*?Overall",
    re.IGNORECASE,
)
_MARKET_DAYS_SUPPLY_SECTION_RE = re.compile(r"Market\s*Days\s*Supply(.{0,200})", re.IGNORECASE | re.DOTALL)
_OVERALL_DAYS_RE = re.compile(r"Overall\D*?(\d+)", re.IGNORECASE)
# Matches either a real number or the literal "N/A" — N/A must be checked
# for explicitly and short-circuit to None, never skipped over while
# hunting for the next digit elsewhere on the page (that's how an unrelated
# stray number from later in the page text was getting misread as the
# Matching days-supply figure when ACV Max itself was showing N/A due to a
# too-thin comparable set).
_MATCHING_DAYS_RE = re.compile(r"Matching\s*(N/A|\d+)", re.IGNORECASE)
# Sanity ceiling for the two day-supply numbers above. When the chart's
# label text overlaps itself in the DOM (a rendering glitch, not a text
# format change), inner_text() can read the same 2-digit number twice back
# to back with no separator — "52" doubled reads as a contiguous "5252" —
# and (\d+) has no way to tell that apart from one real 4-digit value. No
# real day-supply figure is anywhere near a year, so anything above this is
# discarded as a doubled/corrupted read rather than propagated as real data.
_MAX_PLAUSIBLE_DAYS_SUPPLY = 365


def _undouble_digits(raw: str) -> str:
    """If `raw` is exactly two identical halves concatenated (e.g. "5252" =
    "52" + "52", "7979" = "79" + "79") — the doubled-DOM-read pattern
    described above — return just the first half. Otherwise return `raw`
    unchanged. Applied to the regex capture BEFORE int conversion so the
    real underlying number is recovered rather than merely detected."""
    if len(raw) >= 2 and len(raw) % 2 == 0:
        half = len(raw) // 2
        if raw[:half] == raw[half:]:
            return raw[:half]
    return raw


def _is_doubled_digits(n: int) -> bool:
    """True if n's decimal digits are themselves two identical halves
    concatenated — a doubled DOM read that slipped through as one number
    (e.g. undoubling already ran on a differently-shaped duplicate, or the
    duplication happened before the regex ever captured it)."""
    s = str(n)
    return len(s) >= 2 and len(s) % 2 == 0 and s[: len(s) // 2] == s[len(s) // 2 :]
_AVG_MILEAGE_RE = re.compile(r"Avg\.?\s*Mileage\D*?([\d,]+)", re.IGNORECASE)
# The current price's percent-of-market-average, e.g. "$54,987(102%)" in the
# low/avg/high price distribution chart right under Market Days Supply.
_PERCENT_OF_MARKET_RE = re.compile(r"\$[\d,]+\((\d+)%\)", re.IGNORECASE)
_MARKET_RANK_OF_RE = re.compile(r"Rank\D*?of\s+(\d+)", re.IGNORECASE)
# Define Competitive Set filter's search radius, e.g. "DISTANCE" <newline>
# "750" (label immediately before its own value, same layout as the market
# days-supply chart's "Overall62"/"Matching38" — see comment above) — live-
# confirmed in scraper_debug/market_velocity_debug.txt.
_SEARCH_DISTANCE_RE = re.compile(r"DISTANCE\s*[\n\r]+\s*(\d+)", re.IGNORECASE)


def _proof_point_key(label: str) -> str:
    low = label.lower()
    if "j.d. power" in low or "jd power" in low or "j d power" in low:
        return "jd_power"
    if "market avg" in low or "market average" in low:
        return "market_average"
    if "acv max retail" in low or "max retail" in low:
        return "acv_max_retail"
    if "typical listing" in low:
        return "typical_listing_price"
    return re.sub(r"[^a-z0-9]+", "_", low).strip("_") or "unknown"


class ACVMaxScraper(_BrowserSession):
    """Log into ACV MAX, open a vehicle by stock number, and scrape its pricing
    screen (vehicle facts + section-3 proof points).

    Usage:
        with ACVMaxScraper(headless=True) as s:
            data = s.scrape("DT23358A")
    """

    SESSION_FILENAME = "acvmax_session.json"

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("viewport", {"width": 1680, "height": 1200})
        super().__init__(**kwargs)
        self._last_row_text = ""
        self._last_status_code: int | None = None

    # -- auth ------------------------------------------------------------- #

    def is_ready(self) -> bool:
        """True if the saved session lands on the MB-of-Durham inventory SPA."""
        assert self.page is not None
        self.page.goto(ACVMAX_INVENTORY_URL, wait_until="domcontentloaded")
        # my.max.auto polls analytics forever and rarely reaches networkidle —
        # never let that block the readiness check.
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        self.page.wait_for_timeout(2_500)
        if "auth.firstlook.biz" in self.page.url:
            return False
        if _exists(self.page, "acvmax_cas_login_marker", timeout_ms=1_500):
            return False
        if not _exists(self.page, "acvmax_inventory_ready", timeout_ms=8_000):
            return False
        return self.page.get_by_text(ACVMAX_DEALERSHIP, exact=False).count() > 0

    def login(self, *, force: bool = False) -> None:
        assert self.page is not None

        if not force and self.use_saved_session and self.is_ready():
            print("[scraper] reusing saved ACV MAX session")
            return

        self._cas_login(force=force)
        self._select_dealership()
        self._save_session()
        print("[scraper] ACV MAX session ready, saved")

    def _cas_login(self, *, force: bool) -> None:
        assert self.page is not None

        if not force:
            # Already authenticated? A secured page won't bounce to the CAS host.
            self.page.goto(ACVMAX_DEALER_INDEX_URL, wait_until="domcontentloaded")
            self.page.wait_for_load_state("networkidle")
            if "auth.firstlook.biz" not in self.page.url and not _exists(
                self.page, "acvmax_cas_login_marker", timeout_ms=1_500
            ):
                print("[scraper] ACV MAX: existing CAS session still valid")
                return

        username = getattr(credentials, "ACVMAX_USERNAME", "")
        password = getattr(credentials, "ACVMAX_PASSWORD", "")
        if not username or password in ("", "changeme", "your_password_here"):
            raise LoginError(
                "ACVMAX_USERNAME / ACVMAX_PASSWORD are not set in credentials.py"
            )

        print(f"[scraper] CAS login at {ACVMAX_CAS_LOGIN_URL.split('?')[0]}")
        self.page.goto(ACVMAX_CAS_LOGIN_URL, wait_until="domcontentloaded")
        try:
            _first_visible(self.page, "acvmax_username", self.timeout_ms).fill(username)
            _first_visible(self.page, "acvmax_password", self.timeout_ms).fill(password)
            with self.page.expect_navigation(wait_until="domcontentloaded"):
                _first_visible(self.page, "acvmax_login_submit", self.timeout_ms).click()
        except ScraperError:
            self._dump_debug("acvmax-login-form")
            raise

        self.page.wait_for_load_state("networkidle")
        if "auth.firstlook.biz" in self.page.url or _exists(
            self.page, "acvmax_cas_login_marker", timeout_ms=3_000
        ):
            self._dump_debug("acvmax-login-failed")
            raise LoginError(
                "CAS login did not complete — still on auth.firstlook.biz. Check "
                "ACVMAX_USERNAME / ACVMAX_PASSWORD in credentials.py."
            )
        print("[scraper] CAS login OK")

    def _select_dealership(self) -> None:
        assert self.page is not None

        if "my.max.auto" in self.page.url and self.page.get_by_text(
            ACVMAX_DEALERSHIP, exact=False
        ).count():
            return

        self.page.goto(ACVMAX_DEALER_INDEX_URL, wait_until="networkidle")
        try:
            link = _first_visible(self.page, "acvmax_dealer_link", self.timeout_ms)
            with self.page.expect_navigation(wait_until="domcontentloaded"):
                link.click()
        except (ScraperError, PlaywrightTimeoutError):
            self._dump_debug("acvmax-dealer-select")
            raise LoginError(
                f"Could not select the {ACVMAX_DEALERSHIP!r} dealership from the "
                f"dealer list."
            )

        # my.max.auto never reaches networkidle (constant analytics polling),
        # so wait_until="networkidle" above would just burn the full
        # navigation timeout. This bounded, best-effort wait gives the SPA a
        # chance to settle without risking that hang.
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass

        if "my.max.auto" not in self.page.url:
            self._dump_debug("acvmax-dealer-select")
            raise LoginError(
                f"Dealership selection did not hand off to my.max.auto "
                f"(at {self.page.url})."
            )
        _first_visible(self.page, "acvmax_inventory_ready", self.timeout_ms)
        print(f"[scraper] selected dealership: {ACVMAX_DEALERSHIP}")

    # -- search ------------------------------------------------------- #

    def find_vehicle(self, stock_number: str) -> str:
        """Search inventory by stock number; return the vehicle id (the digits in
        /inventory/<id>). Raises VehicleNotFoundError on 0 or >1 matches."""
        assert self.page is not None
        stock = stock_number.strip().upper()
        print(f"[scraper] searching inventory for stock #{stock}")

        self.page.goto(ACVMAX_INVENTORY_URL, wait_until="networkidle")
        _first_visible(self.page, "acvmax_inventory_ready", self.timeout_ms)
        self.page.wait_for_timeout(2_000)

        search_input = _first_visible(self.page, "acvmax_quick_search", self.timeout_ms)
        search_input.fill("")  # explicit clear — do not rely on the box being empty
        self.page.wait_for_timeout(200)
        search_input.fill(stock)  # .fill() replaces the value atomically instead of relying on keystroke-by-keystroke state
        self.page.wait_for_timeout(3_000)
        self.page.wait_for_load_state("networkidle")

        actual_value = search_input.input_value()
        if actual_value.strip().upper() != stock.strip().upper():
            self._dump_debug(f"acvmax-search-box-mismatch-{stock}")
            raise VehicleNotFoundError(
                f"Stock #{stock}: search box shows {actual_value!r} after fill — "
                f"search did not take, refusing to scan results."
            )

        rows = self.page.evaluate(
            """() => [...document.querySelectorAll('a[href]')]
                .map(a => ({href: a.getAttribute('href'),
                            row: (a.closest('tr') || a.parentElement)?.innerText || ''}))
                .filter(x => /^\\/inventory\\/\\d+$/.test(x.href || ''))"""
        )
        # De-dupe by href, keep the longest row text seen for each.
        by_href: dict[str, str] = {}
        for r in rows:
            by_href[r["href"]] = max(by_href.get(r["href"], ""), r["row"], key=len)

        stock_bare = stock.lstrip("#")
        matches = [
            href for href, txt in by_href.items() if stock_bare in txt.upper().replace("#", "")
        ]

        if len(matches) != 1:
            self._dump_debug(f"acvmax-search-{stock_bare}")
            raise VehicleNotFoundError(
                f"Stock #{stock_bare}: expected 1 inventory match, got {len(matches)} "
                f"({matches[:5]}). See {DEBUG_DIR}/ for a snapshot."
            )

        href = matches[0]
        self._last_row_text = by_href[href]
        # The result row carries a "Status: <n>" code (e.g. 10, 11, 12, 16).
        sc = re.search(r"Status:\s*(\d+)", self._last_row_text, re.IGNORECASE)
        self._last_status_code = int(sc.group(1)) if sc else None
        vehicle_id = href.rsplit("/", 1)[-1]
        print(
            f"[scraper] matched vehicle id {vehicle_id}"
            + (f" (status {self._last_status_code})" if self._last_status_code is not None else "")
        )
        return vehicle_id

    # -- shared merchandising-iframe attach ------------------------- #

    def _attach_merchandising_frame(
        self,
        vehicle_id: str,
        hint: str,
        *,
        error_cls: type[ScraperError],
        no_iframe_tag: str,
        no_frame_tag: str,
    ) -> Frame:
        """Wait for a merchandising iframe whose src contains `hint` to attach
        and return its live Frame.

        Every /inventory/{id}/<tab> page (pricing, options, equipment, ...) is
        an empty outer shell — the real content renders inside one of these
        merchandising iframes (confirmed from live debug dumps: pricing's is
        .../merchandising/PricingAnalysis/..., options' is
        .../merchandising/Workflow/LoadPackages.aspx, equipment's is
        .../merchandising/Workflow/LoadEquipment.aspx). Reading `self.page`
        directly instead of the returned frame gets that empty shell.
        """
        assert self.page is not None
        try:
            handle = self.page.wait_for_selector(
                f"iframe[src*='{hint}']", timeout=self.timeout_ms
            )
        except PlaywrightTimeoutError as exc:
            self._dump_debug(no_iframe_tag)
            raise error_cls(
                f"Merchandising iframe ({hint}) never appeared for vehicle {vehicle_id}."
            ) from exc

        # The <iframe> element exists; its Frame may still be attaching. Prefer
        # the element's own content_frame(), fall back to polling page.frames.
        frame = handle.content_frame() if handle else None
        for _ in range(20):
            if frame and hint in (frame.url or ""):
                break
            self.page.wait_for_timeout(500)
            frame = next(
                (f for f in self.page.frames if hint in (f.url or "")), frame
            )
        if frame is None or hint not in (frame.url or ""):
            self._dump_debug(no_frame_tag)
            raise error_cls(
                f"Merchandising iframe ({hint}) for {vehicle_id} never "
                f"attached with a usable URL."
            )
        try:
            frame.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        return frame

    # -- pricing ---------------------------------------------------- #

    def open_pricing(self, vehicle_id: str) -> tuple[Frame, str]:
        assert self.page is not None
        url = f"{ACVMAX_INVENTORY_URL}/{vehicle_id}/pricing"

        # Race fix: a direct navigation straight to a new vehicle_id can leave
        # the SPA rendering the *previous* vehicle's pricing data instead of
        # the one just requested. A cache-busting query param on the initial
        # goto plus a hard reload right after forces the SPA to fetch and
        # render this vehicle fresh rather than serving stale in-memory state.
        #
        # Both still use domcontentloaded, not networkidle: my.max.auto never
        # reaches networkidle (constant analytics polling), so wait_until=
        # "networkidle" here would just burn the full navigation timeout on
        # every single call. The bounded, best-effort wait below gives the SPA
        # a chance to settle without risking that hang.
        self.page.goto(f"{url}?_r={int(time.time())}", wait_until="domcontentloaded")
        self.page.reload(wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass

        frame = self._attach_merchandising_frame(
            vehicle_id,
            ACVMAX_PRICING_FRAME_HINT,
            error_cls=PricingNotFoundError,
            no_iframe_tag=f"acvmax-no-pricing-iframe-{vehicle_id}",
            no_frame_tag=f"acvmax-pricing-frame-{vehicle_id}",
        )
        try:
            frame.get_by_text("Pricing Proof Points", exact=False).first.wait_for(
                timeout=self.timeout_ms
            )
        except PlaywrightTimeoutError as exc:
            self._dump_debug(f"acvmax-pricing-empty-{vehicle_id}")
            raise PricingNotFoundError(
                f"Pricing iframe for {vehicle_id} loaded but showed no proof points."
            ) from exc
        # Identity-verification settle wait: scrape_pricing() reads this page's
        # stock #/VIN right after this call returns and raises
        # VehicleIdentityError on a mismatch. Bumped from 1.5s to 15s so a
        # freshly reloaded SPA has time to finish rendering the requested
        # vehicle's own data before that check runs, instead of racing it.
        self.page.wait_for_timeout(15_000)
        return frame, url

    def scrape_pricing(
        self,
        stock_number: str,
        *,
        vehicle_id: str | None = None,
        expected_vin: str | None = None,
    ) -> dict[str, Any]:
        """Scrape the ACV Max pricing screen for `stock_number`.

        `vehicle_id`, when given (already resolved by the caller's own
        find_vehicle() call, or from a prior inventory crawl), skips the
        inventory search entirely and navigates straight to
        /inventory/{vehicle_id}/pricing. Without it, this method searches
        internally via find_vehicle() as before. Passing a pre-resolved id
        matters because find_vehicle() is a fresh quick-search each time it
        runs — two independent searches for the same stock number are not
        guaranteed to resolve to the same row, and skipping the second one
        removes that race entirely.

        Note: when `vehicle_id` is supplied, find_vehicle()'s own inventory
        search never runs, so status_code (normally read off the search-
        result row) comes back None. Callers on this fast path that need it
        should get it from wherever they already had `vehicle_id` (e.g. a
        prior crawl snapshot).

        This method never determines certification status — `certified` /
        `certified_detail` in the returned dict are always the None
        placeholders _parse_pricing_text() sets. A vehicle's certification
        must come from its status_code (10/16 = MB CPO, 11 = Hendrick
        Certified — see aggregator.CERTIFIED_STATUS_CODES), never from
        anything scraped here: this used to also regex a "certified"/"CPO"
        substring out of _last_row_text (the inventory search-result row,
        captured in find_vehicle()) and use that as the vehicle's own
        certification — unreliable on its own terms (blank whenever a
        pre-resolved `vehicle_id` is passed in, which skips find_vehicle()
        and is the common path throughout aggregator.py), and reported as
        actually picking up the dealer's "Certified" competitive-set filter
        state from the pricing screen rather than the vehicle's own status —
        a filter set to "All" to widen a thin comparable set has nothing to
        do with whether THIS vehicle is certified.

        `expected_vin`, when given (the VIN the inventory crawler already has
        for this stock number), is checked against both the resolved stock
        number and VIN on the loaded pricing page. A mismatch on either raises
        VehicleIdentityError instead of silently returning another vehicle's
        pricing data — e.g. a stale cached page, or ACV Max's search landing on
        the wrong row.
        """
        assert self.page is not None
        if vehicle_id:
            print(
                f"[scraper] using pre-resolved vehicle id {vehicle_id} for "
                f"stock #{stock_number.strip().lstrip('#')} — skipping inventory search"
            )
        else:
            vehicle_id = self.find_vehicle(stock_number)
        frame, url = self.open_pricing(vehicle_id)

        text = frame.locator("body").inner_text()
        list_price = None
        for hid in ("#ListPrice", "#PPInternetPrice", "#fllistpriceoriginal"):
            loc = frame.locator(hid)
            if loc.count():
                list_price = _money(loc.first.get_attribute("value"))
                if list_price:
                    break

        stock_from_grid = frame.evaluate(
            """() => {
                const rows = [...document.querySelectorAll('#UnitsInStockDesc tr[role=row]')];
                for (const r of rows) {
                    const v = r.querySelector("td[aria-describedby='UnitsInStockDesc_VIN']");
                    const s = r.querySelector("td[aria-describedby='UnitsInStockDesc_StockNumber']");
                    if (v && s) return {vin: v.innerText.trim(), stock: s.innerText.trim()};
                }
                return null;
            }"""
        )

        data = self._parse_pricing_text(text, stock_number)
        data["vehicle_id"] = vehicle_id
        data["source_url"] = url
        data["status_code"] = self._last_status_code
        if list_price is not None:
            data["current_internet_price"] = list_price
        if stock_from_grid:
            data["vin"] = data["vin"] or stock_from_grid.get("vin")
            data["stock_number"] = stock_from_grid.get("stock") or data["stock_number"]

        print(
            f"[scraper] market velocity: {data.get('matching_count')} matching, "
            f"{data.get('overall_market_days')} overall days, "
            f"{data.get('matching_market_days')} matching days, "
            f"rank {data.get('market_rank')} of {data.get('market_rank_of')}"
        )

        # certified / certified_detail are deliberately left as the None
        # placeholders _parse_pricing_text() set — see this method's
        # docstring for why nothing on this screen is trusted for that.

        if data["current_internet_price"] is None and not data["pricing_proof_points"]:
            self._dump_debug(f"acvmax-empty-parse-{vehicle_id}")

        requested_stock = stock_number.strip().lstrip("#").upper()
        resolved_stock = (data.get("stock_number") or "").strip().upper()
        if resolved_stock and requested_stock and resolved_stock != requested_stock:
            raise VehicleIdentityError(
                f"Requested stock #{requested_stock} but the pricing page resolved "
                f"to stock #{resolved_stock} — wrong vehicle, refusing to proceed."
            )
        if expected_vin:
            resolved_vin = (data.get("vin") or "").strip().upper()
            if resolved_vin and resolved_vin != expected_vin.strip().upper():
                raise VehicleIdentityError(
                    f"Stock #{requested_stock}: pricing page VIN {resolved_vin!r} "
                    f"does not match expected VIN {expected_vin!r} — wrong "
                    f"vehicle, refusing to proceed."
                )
        return data

    # -- parsing --------------------------------------------------- #

    @staticmethod
    def _parse_pricing_text(text: str, stock_hint: str) -> dict[str, Any]:
        raw_lines = [ln.rstrip() for ln in text.splitlines()]
        lines = [_clean(ln) for ln in raw_lines if _clean(ln)]

        def after_label(label: str) -> str | None:
            pat = re.compile(rf"^{re.escape(label)}\s*:?\s*(.*)$", re.IGNORECASE)
            for ln in lines:
                m = pat.match(ln)
                if m and m.group(1).strip():
                    return m.group(1).strip()
            return None

        year_make_model = lines[0] if lines else None
        trim_body = lines[1] if len(lines) > 1 and "VIN" not in lines[1].upper() else None

        vin = None
        m = re.search(r"VIN:\s*(" + _VIN_RE.pattern + r")", text)
        if m:
            vin = m.group(1)

        stock_number = after_label("Stock #") or stock_hint.strip().lstrip("#")
        days_on_lot = _int(after_label("Days"))
        exterior_color = after_label("Color")
        # ACV Max's pricing header sometimes also carries interior color
        # (Ext/Int Color are shown as separate fields); exact label varies,
        # so try both. None if the header never shows it.
        interior_color = after_label("Interior Color") or after_label("Int Color")
        mileage = _int(after_label("Mileage"))
        unit_cost = _money(after_label("Unit Cost"))

        proof_points = ACVMaxScraper._parse_proof_points(lines)
        by_key = {p["key"]: p for p in proof_points}

        current_price = None
        implied = [
            p["benchmark_price"] + p["gap"]
            if p["direction"] == "above"
            else p["benchmark_price"] - p["gap"]
            if p["direction"] == "below"
            else p["benchmark_price"]
            for p in proof_points
            if p["benchmark_price"] is not None and p["gap"] is not None
        ]
        if implied:
            current_price = max(set(implied), key=implied.count)

        market_velocity = ACVMaxScraper._parse_market_velocity(text)

        return {
            "source": "acvmax",
            "source_url": None,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "dealership": ACVMAX_DEALERSHIP,
            "vehicle_id": None,
            "status_code": None,
            "vin": vin,
            "stock_number": stock_number,
            "year_make_model": year_make_model,
            "trim_body": trim_body,
            "mileage": mileage,
            "exterior_color": exterior_color,
            "interior_color": interior_color,
            "days_on_lot": days_on_lot,
            "unit_cost": unit_cost,
            "current_internet_price": current_price,
            "certified": None,
            "certified_detail": None,
            "pricing_proof_points": proof_points,
            "proof_points_by_key": by_key,
            # Convenience flat mirrors of the same four benchmark prices
            # already in proof_points_by_key, named for the icon they sit
            # next to on the Pricing Proof Points panel.
            "market_avg_price": (by_key.get("market_average") or {}).get("benchmark_price"),
            "jdpower_price": (by_key.get("jd_power") or {}).get("benchmark_price"),
            "typical_listing_price": (by_key.get("typical_listing_price") or {}).get("benchmark_price"),
            "acvmax_retail": (by_key.get("acv_max_retail") or {}).get("benchmark_price"),
            **market_velocity,
            "raw_pricing_text": text,
        }

    @staticmethod
    def _parse_market_velocity(text: str) -> dict[str, Any]:
        """"Define Competitive Set" / market days-supply chart / "Price In
        the Game" widgets from the pricing screen — comparable-set size, how
        fast this exact configuration turns versus the overall market, and
        where this vehicle's price ranks within its matching set.

        Each field is parsed independently straight out of the frame's raw
        body text (see the module-level regexes above) and is None — never a
        raised error — when its widget isn't on the page for this vehicle
        (e.g. a thin or absent comparable set). market_rank is always None —
        see _MARKET_RANK_OF_RE's comment above for why that one number can't
        be read from text at all."""
        overall_count = matching_count = None
        m = _COMPETITIVE_SET_RE.search(text)
        if m:
            matching_count = _int(m.group(1))
            overall_count = _int(m.group(2))

        overall_market_days = matching_market_days = None
        m_section = _MARKET_DAYS_SUPPLY_SECTION_RE.search(text)
        if m_section:
            section = m_section.group(1)
            m_overall = _OVERALL_DAYS_RE.search(section)
            if m_overall:
                overall_market_days = _int(_undouble_digits(m_overall.group(1)))
            m_matching = _MATCHING_DAYS_RE.search(section)
            if m_matching and m_matching.group(1).upper() != "N/A":
                matching_market_days = _int(_undouble_digits(m_matching.group(1)))

        if overall_market_days is not None and overall_market_days > _MAX_PLAUSIBLE_DAYS_SUPPLY:
            print(
                f"[scraper] WARNING: implausible overall_market_days="
                f"{overall_market_days} (>{_MAX_PLAUSIBLE_DAYS_SUPPLY}) — discarding",
                file=sys.stderr,
            )
            overall_market_days = None
        if matching_market_days is not None and matching_market_days > _MAX_PLAUSIBLE_DAYS_SUPPLY:
            print(
                f"[scraper] WARNING: implausible matching_market_days="
                f"{matching_market_days} (>{_MAX_PLAUSIBLE_DAYS_SUPPLY}) — discarding",
                file=sys.stderr,
            )
            matching_market_days = None

        # Secondary sanity check, on whatever survived the ceiling above: an
        # exact-double relationship between the two figures, a suspiciously
        # low reading, or a self-doubled digit pattern in either one are all
        # signs of a corrupted DOM read rather than real data. Both values
        # are discarded together — a corrupted pair can't be trusted to
        # salvage just one half of it.
        if overall_market_days is not None and matching_market_days is not None:
            hi = max(overall_market_days, matching_market_days)
            lo = min(overall_market_days, matching_market_days)
            doubled_relationship = lo > 0 and abs(hi - 2 * lo) <= 0.05 * hi
            suspiciously_low = overall_market_days <= 0 or matching_market_days <= 0
            repeated_pattern = (
                _is_doubled_digits(overall_market_days)
                or _is_doubled_digits(matching_market_days)
            )
            if doubled_relationship or suspiciously_low or repeated_pattern:
                print(
                    f"[scraper] WARNING: implausible day-supply pair "
                    f"overall={overall_market_days} matching={matching_market_days} "
                    f"(doubled_relationship={doubled_relationship}, "
                    f"suspiciously_low={suspiciously_low}, "
                    f"repeated_pattern={repeated_pattern}) — discarding both",
                    file=sys.stderr,
                )
                overall_market_days = None
                matching_market_days = None

        market_velocity_gap = None
        if overall_market_days is not None and matching_market_days is not None:
            market_velocity_gap = overall_market_days - matching_market_days

        avg_mileage = None
        m = _AVG_MILEAGE_RE.search(text)
        if m:
            avg_mileage = _int(m.group(1))

        market_rank = None
        market_rank_of = None
        m = _MARKET_RANK_OF_RE.search(text)
        if m:
            market_rank_of = _int(m.group(1))

        percent_of_market = None
        m = _PERCENT_OF_MARKET_RE.search(text)
        if m:
            percent_of_market = _int(m.group(1))

        search_distance = None
        m = _SEARCH_DISTANCE_RE.search(text)
        if m:
            search_distance = _int(m.group(1))

        return {
            "overall_count": overall_count,
            "matching_count": matching_count,
            "overall_market_days": overall_market_days,
            "matching_market_days": matching_market_days,
            "market_velocity_gap": market_velocity_gap,
            "avg_mileage": avg_mileage,
            "market_rank": market_rank,
            "market_rank_of": market_rank_of,
            "percent_of_market": percent_of_market,
            "search_distance": search_distance,
        }

    @staticmethod
    def _parse_proof_points(lines: list[str]) -> list[dict[str, Any]]:
        try:
            start = next(
                i for i, ln in enumerate(lines) if ln.lower() == "pricing proof points"
            ) + 1
        except StopIteration:
            return []
        stop_words = {"vehicle history report", "print window sticker"}
        block = []
        for ln in lines[start:]:
            if ln.lower() in stop_words:
                break
            block.append(ln)

        out: list[dict[str, Any]] = []
        i = 0
        while i < len(block):
            bm = _PP_BENCHMARK_RE.match(block[i])
            if not bm:
                i += 1
                continue
            benchmark = _money(bm.group(1))
            gap = direction = label = None
            if i + 1 < len(block):
                cm = _PP_CURRENT_RE.match(block[i + 1])
                if cm:
                    gap = _money(cm.group(1))
            if i + 2 < len(block):
                dm = _PP_DIRECTION_RE.match(block[i + 2])
                if dm:
                    direction = dm.group(1).lower()
                    label = _clean(dm.group(2))
            if direction is None:
                i += 1
                continue
            out.append(
                {
                    "key": _proof_point_key(label or ""),
                    "label": label,
                    "benchmark_price": benchmark,
                    "gap": gap,
                    "direction": direction,
                    "summary": f"current price is ${_fmt(gap)} {direction} {label}"
                    if gap is not None
                    else f"current price is {direction} {label}",
                }
            )
            i += 3
        return out

    # -- carfax ----------------------------------------------------- #

    def scrape_carfax(self, *, acvmax_mileage: int | None = None) -> dict[str, Any]:
        """Open the Carfax report from the currently-loaded pricing page, scrape
        it, close the tab, and return a structured dict.

        Precondition: self.page is on a /inventory/<id>/pricing page with the
        PricingAnalysis iframe already loaded (i.e. call scrape_pricing() or
        open_pricing() first).

        acvmax_mileage: the ACV MAX current mileage for the cross-check in step 7.
        If omitted it is read from the pricing iframe header.
        """
        assert self.page is not None
        frame = next(
            (f for f in self.page.frames if ACVMAX_PRICING_FRAME_HINT in (f.url or "")),
            None,
        )
        if frame is None:
            self._dump_debug("carfax-no-pricing-frame")
            raise CarfaxError(
                "scrape_carfax: no pricing iframe on the current page — call "
                "scrape_pricing()/open_pricing() first."
            )

        frame_text = ""
        try:
            frame_text = frame.locator("body").inner_text()
        except Exception:  # noqa: BLE001
            pass
        if acvmax_mileage is None:
            m = re.search(r"Mileage:\s*([\d,]+)", frame_text)
            acvmax_mileage = int(m.group(1).replace(",", "")) if m else None
        vm = re.search(r"VIN:\s*(" + _VIN_RE.pattern + r")", frame_text)
        vin = vm.group(1) if vm else None

        cf, open_path = self._open_carfax_report(frame, vin)
        print(f"[scraper] carfax: opened via {open_path}")

        try:
            cf.wait_for_load_state("domcontentloaded")
            try:
                cf.wait_for_load_state("networkidle", timeout=25_000)
            except PlaywrightTimeoutError:
                pass
            try:
                cf.get_by_text(
                    re.compile(
                        r"Ownership History|Last reported odometer|CARFAX Vehicle History",
                        re.IGNORECASE,
                    )
                ).first.wait_for(timeout=self.timeout_ms)
            except PlaywrightTimeoutError:
                pass
            cf.wait_for_timeout(3_000)

            text = cf.locator("body").inner_text()
            try:
                state = cf.evaluate("() => window.__INITIAL__DATA__ || null")
            except Exception:
                state = None
            try:
                html = cf.content()
            except Exception:
                html = ""

            data = self._parse_carfax(text, state, cf.url, cf.title(), html)
            data["carfax_open_path"] = open_path
            data["carfax_image_path"] = self._capture_carfax_screenshot(cf, vin)
            if not data["number_of_owners"] and not data["last_reported_odometer"]:
                self._dump_debug(f"carfax-parse-empty-{open_path.replace(' ', '-')}", page=cf)
        finally:
            try:
                if cf is not None:
                    cf.close()
            except Exception:
                pass
        # focus is back on self.page (it was never switched away)

        # Step 7: odometer cross-check.
        last_odo = data["last_reported_odometer"]
        gap = (
            acvmax_mileage - last_odo
            if (acvmax_mileage is not None and last_odo is not None)
            else None
        )
        data["acvmax_mileage"] = acvmax_mileage
        data["mileage_gap_vs_carfax"] = gap
        if gap is not None and gap > 2_000:
            data["warranty_current"] = "unverified"
            data["warranty_claimable"] = False
            data["warranty_note"] = (
                f"ACV MAX mileage ({acvmax_mileage:,}) exceeds Carfax last reported "
                f"odometer ({last_odo:,}) by {gap:,} mi (> 2,000). Warranty coverage "
                f"is NOT claimed in this data package."
            )
        else:
            data["warranty_current"] = "verified" if gap is not None else "unknown"
            data["warranty_claimable"] = (
                data["warranty_status"] == "active"
                and data["warranty_current"] != "unverified"
            )
            data["warranty_note"] = None
        return data

    @staticmethod
    def _capture_carfax_screenshot(cf: Page, vin: str | None) -> str | None:
        """Full-page PNG of the open Carfax report tab, saved to
        carfax_cache/<vin>.png for vision_parser.parse_carfax_image(). Returns
        the path on success, None on any capture failure — this is a bonus
        data source for the vision parser, never something the Carfax scrape
        itself should fail over. full_page=True matters here: a Carfax report
        is long and scrolls, and a viewport-only screenshot would silently
        drop most of the report below the fold."""
        if not vin:
            return None
        path = CARFAX_CACHE_DIR / f"{vin}.png"
        try:
            cf.screenshot(path=str(path), type="png", full_page=True)
        except Exception as exc:  # noqa: BLE001 - best-effort only
            print(f"[scraper] carfax screenshot capture failed for {vin}: {exc}")
            return None
        return str(path)

    def _open_carfax_report(self, frame: Frame, vin: str | None) -> tuple[Page, str]:
        """Open the Carfax report in a new tab. Tries, in order:
          1. connect.carfax.com/dir?key=<data-snapshotkey>   (deterministic goto)
          2. clicking the 'View CARFAX Report' link in the widget's shadow DOM
          3. carfaxonline.com/vhr/<VIN>                       (last resort)
        Paths 1-2 are retried once (5s apart). Returns (carfax_page, path_label).
        Raises CarfaxError only if every path fails.
        """
        assert self.page is not None

        # The passed frame can go stale after SPA churn — re-derive the live one.
        frame = next(
            (f for f in self.page.frames if ACVMAX_PRICING_FRAME_HINT in (f.url or "")),
            frame,
        )

        # Let the <vehicle-history-report> web component attach, then give its
        # externally-scripted (snapshot.carfax.com) shadow DOM time to render.
        try:
            frame.locator("vehicle-history-report").first.wait_for(
                state="attached", timeout=15_000
            )
        except PlaywrightTimeoutError:
            pass
        frame.wait_for_timeout(4_000)

        key_loc = frame.locator("[data-snapshotkey]").first
        link = frame.locator(
            "vehicle-history-report a:has-text('View CARFAX Report'), "
            "vehicle-history-report a[href*='connect.carfax.com'], "
            "a[href*='connect.carfax.com/dir']"
        ).first

        def _grab_key(rounds: int) -> str | None:
            for _ in range(rounds):
                try:
                    if key_loc.count():
                        k = key_loc.get_attribute("data-snapshotkey")
                        if k:
                            return k
                except Exception:  # noqa: BLE001 - keep polling
                    pass
                self.page.wait_for_timeout(1_000)
            return None

        def _click_link() -> Page | None:
            try:
                if not link.count():
                    return None
                with self.page.context.expect_page(timeout=self.timeout_ms) as new_page:
                    link.first.click(timeout=10_000)
                return new_page.value
            except (PlaywrightTimeoutError, ScraperError):
                return None
            except Exception as exc:  # noqa: BLE001
                print(f"[scraper] carfax: link click error ({exc})")
                return None

        for attempt in (1, 2):
            # (1) snapshot key -> deterministic report URL
            key = _grab_key(30 if attempt == 1 else 10)
            if key:
                cf = self.page.context.new_page()
                cf.goto(
                    f"https://connect.carfax.com/dir?key={key}",
                    wait_until="domcontentloaded",
                )
                return cf, "snapshot key"

            # (2) click the shadow-DOM 'View CARFAX Report' link
            cf = _click_link()
            if cf is not None:
                return cf, "shadow DOM link"

            if attempt == 1:
                print("[scraper] carfax: attempt 1 failed; retrying in 5s ...")
                self.page.wait_for_timeout(5_000)

        # (3) last resort — direct VIN URL
        if vin:
            print(
                "[scraper] carfax: snapshot key and shadow-DOM link both "
                "unavailable; using direct VIN fallback"
            )
            cf = self.page.context.new_page()
            cf.goto(
                f"https://www.carfaxonline.com/vhr/{vin}",
                wait_until="domcontentloaded",
            )
            return cf, "direct VIN fallback"

        self._dump_debug("carfax-link-not-found")
        raise CarfaxError(
            "Could not open the Carfax report: the data-snapshotkey, the "
            "shadow-DOM link, and the direct VIN fallback were all unavailable "
            "after a retry."
        )

    # Detailed-history source names that are not service/repair facilities.
    _CARFAX_NON_FACILITY = re.compile(
        r"motor vehicle dept|\bdmv\b|inspection station|auto auction|online listing"
        r"|\bnhtsa\b|lien(holder)?|^title\b|repossess|insurance|fleet"
        r"|dealer inventory|car rental|leasing|\busa,? llc\b|manufacturer",
        re.IGNORECASE,
    )

    @staticmethod
    def _carfax_service_facilities(state: Any) -> list[str]:
        """Unique service/repair-shop names from the report's embedded JSON state
        (window.__INITIAL__DATA__), excluding DMV / auction / listing sources."""
        out: list[str] = []
        seen: set[str] = set()
        try:
            blocks = state["vhr"]["detailsSection"]["ownerBlocks"]["ownerBlocks"]
        except (KeyError, TypeError):
            return out
        for blk in blocks or []:
            for rec in ((blk.get("records") or {}).get("records") or []):
                comments = " ".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in (rec.get("comments") or {}).get("commentLines", [])
                ) if isinstance(rec.get("comments"), dict) else str(rec.get("comments") or "")
                name = None
                for sl in ((rec.get("source") or {}).get("sourceLines") or []):
                    url_txt = (((sl.get("sourceUrlLine") or {}).get("urlDisplay") or {})
                               .get("text"))
                    txt_txt = (sl.get("sourceTextLine") or {}).get("text")
                    cand = _clean(url_txt or txt_txt)
                    if (
                        cand and " " in cand
                        and not re.match(r"https?://|\S+\.\w{2,5}/?$", cand)
                        and not re.match(r"^[\d(]", cand)
                        and not re.fullmatch(r"[A-Za-z .]+,\s*[A-Z]{2}", cand)  # "City, ST"
                    ):
                        name = cand
                        break
                if not name or name in _US_STATES:
                    continue
                if ACVMaxScraper._CARFAX_NON_FACILITY.search(name + " " + comments):
                    continue
                if name.lower() not in seen:
                    seen.add(name.lower())
                    out.append(name)
        return out

    _STRUCTURAL_DAMAGE_HEADING_RE = re.compile(r"structural damage", re.IGNORECASE)
    _STRUCTURAL_DAMAGE_WINDOW_CHARS = 200

    @staticmethod
    def _parse_structural_damage(text: str) -> bool | None:
        """Read the Carfax 'Structural Damage' verdict.

        The Carfax report has two places "Structural Damage" appears:
          * the "Damage Brands" legend/glossary — a pipe-separated line like
            "Damage Brands — Salvage | Junk | Rebuilt | Structural Damage | ..."
            that appears on every report and names no verdict at all.
          * the actual verdict — "Structural Damage — No Issues Reported" or
            "Structural Damage — Issues Reported".

        For each occurrence of "structural damage" (case-insensitive), the line
        it's on is checked for a "|" first — a hit there is the legend, and is
        skipped outright rather than read as a flag. Otherwise, only the next
        _STRUCTURAL_DAMAGE_WINDOW_CHARS characters after the match are searched
        for the verdict phrase, so unrelated later text can't be misread as
        this heading's verdict.

        Returns True (no issues reported — clean), False (issues reported —
        flagged), or None if no verdict could be found (never silently treated
        as a flag)."""
        for m in ACVMaxScraper._STRUCTURAL_DAMAGE_HEADING_RE.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line_end = len(text) if line_end == -1 else line_end
            line = text[line_start:line_end]
            if "|" in line:
                continue  # the "Damage Brands" legend line, not a verdict

            window_low = text[
                m.end(): m.end() + ACVMaxScraper._STRUCTURAL_DAMAGE_WINDOW_CHARS
            ].lower()
            if "no issues reported" in window_low:
                return True
            if "issues reported" in window_low:
                return False
            # neither phrase in this window — keep looking for another
            # non-legend occurrence rather than guessing from this one
        return None

    @staticmethod
    def _parse_carfax(
        text: str, state: Any, url: str, title: str, html: str = ""
    ) -> dict[str, Any]:
        low = text.lower()

        def has(*phrases: str) -> bool:
            return any(p.lower() in low for p in phrases)

        vm = re.search(r"VIN:\s*(" + _VIN_RE.pattern + r")", text)
        vin = vm.group(1) if vm else None

        # Owners
        owners: int | None = None
        m = re.search(r"(\d+)\s+Previous Owners?", text, re.IGNORECASE)
        if m:
            owners = int(m.group(1))
        else:
            nums = {int(n) for n in re.findall(r"\bOwner\s+(\d+)\b", text)}
            owners = max(nums) if nums else None

        # Owner type(s)
        tblock = _slice_between(
            text, "Type of owner",
            ("Estimated length of ownership", "Owned in the following",
             "Last reported odometer", "Detailed History"),
        )
        owner_types = [
            t.capitalize()
            for t in re.findall(
                r"\b(Personal|Lease|Fleet|Rental|Commercial|Government|Taxi|Police)\b",
                tblock, re.IGNORECASE,
            )
        ]
        lease = any(t.lower() == "lease" for t in owner_types)
        if owner_types:
            base = owner_types[-1].lower()
            owner_type = {
                "personal": "personal lease" if lease else "personal use",
                "lease": "personal lease",
                "fleet": "fleet",
                "rental": "rental",
                "commercial": "fleet",
                "government": "fleet",
            }.get(base, base)
        elif "personal vehicle" in low:
            owner_type = "personal lease" if "lease" in low else "personal use"
        else:
            owner_type = None

        # Odometer readings
        oblock = _slice_between(
            text, "Last reported odometer",
            ("Detailed History", "Glossary", "Ownership History", "Title History"),
        )
        odos = [
            int(x.replace(",", ""))
            for x in re.findall(r"\b(\d{1,3}(?:,\d{3})+|\d{4,7})\b", oblock)
        ]
        last_reported_odometer = max(odos) if odos else None

        # Miles per year
        mblock = _slice_between(
            text, "miles driven per year",
            ("Last reported odometer", "Detailed History", "Ownership History"),
        )
        mpy: int | None = None
        m = re.search(r"([\d,]+)\s*(?:per year|/\s*yr|/\s*year)", mblock, re.IGNORECASE)
        m = m or re.search(r"([\d,]{3,})", mblock)
        if m:
            try:
                mpy = int(m.group(1).replace(",", ""))
            except ValueError:
                mpy = None
        low_mileage = mpy is not None and mpy < 10_000

        no_accidents = has(
            "no accidents reported", "no accidents or damage reported",
            "no accident or damage reported",
        )
        no_structural_damage = ACVMaxScraper._parse_structural_damage(text)
        no_total_loss = has("no total loss reported")

        if no_structural_damage is False:
            print(
                f"[carfax] WARNING: structural damage flagged for VIN "
                f"{vin or 'unknown'} — verify this is not a legend parsing error",
                file=sys.stderr,
            )

        # Warranty
        wblock = _slice_between(
            text, "Basic Warranty",
            ("Title History", "Ownership History", "Detailed History"),
        )
        warranty_status = None
        if re.search(r"expired|estimated to have expired", wblock, re.IGNORECASE):
            warranty_status = "expired"
        elif re.search(
            r"\bactive\b|in effect|still (?:in effect|covered)|remaining",
            wblock, re.IGNORECASE,
        ):
            warranty_status = "active"
        wm = re.search(
            r"(\d+)\s*months?(?:\s+or\s+[\d,]+\s*miles?)?\s+remaining"
            r"|(\d+)\s*months?[^.]{0,40}?(?:remaining|left|of coverage)",
            wblock, re.IGNORECASE,
        )
        wmi = re.search(
            r"([\d,]+)\s*miles?(?:\s+or\s+\d+\s*months?)?\s+remaining"
            r"|([\d,]+)\s*miles?[^.]{0,40}?(?:remaining|left|of coverage)",
            wblock, re.IGNORECASE,
        )
        def _grp(m: re.Match[str] | None) -> str | None:
            return next((g for g in m.groups() if g), None) if m else None

        warranty_months_remaining = int(_grp(wm)) if _grp(wm) else None
        _wmi = _grp(wmi)
        warranty_miles_remaining = int(_wmi.replace(",", "")) if _wmi else None

        # Titled states
        states: set[str] = set()
        sblock = _slice_between(
            text, "Owned in the following states",
            ("Estimated miles driven per year", "Last reported odometer",
             "Detailed History"),
        )
        for cand in re.findall(r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)*", sblock):
            if cand in _US_STATES:
                states.add(cand)
        for m in re.finditer(r"Last Owned in ([A-Z][a-zA-Z ]+?)(?:\n|$)", text):
            if m.group(1).strip() in _US_STATES:
                states.add(m.group(1).strip())
        for m in re.finditer(r",\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s*(?:\n|$)", text):
            if m.group(1) in _US_STATES:
                states.add(m.group(1))
        titled_states = sorted(states)

        # Service facilities / all-Mercedes check (from embedded JSON state)
        facs = ACVMaxScraper._carfax_service_facilities(state)
        mb_pat = re.compile(r"mercedes|-benz|\bbenz\b|motorcars|\bMB of\b", re.IGNORECASE)
        all_service_mercedes_benz = bool(facs) and all(mb_pat.search(f) for f in facs)

        # Bonus fields
        m = re.search(
            r"CARFAX (?:Retail )?Value\s*\n?\s*\$?([\d,]+)", text, re.IGNORECASE
        )
        carfax_value = int(m.group(1).replace(",", "")) if m else None
        m = re.search(r"(\d+)\s+Service History Records", text, re.IGNORECASE)
        service_record_count = int(m.group(1)) if m else None

        # Window sticker link detection — non-MB trade-ins often have an OEM
        # sticker (Ford/GM/Mopar/Toyota/Honda) linked from the Carfax report even
        # when AutoiPacket can't produce one. Just capture the URL here; the
        # fetch+parse fallback (_parse_oem_sticker) runs from aggregator.py.
        sticker_url, sticker_hit = _find_window_sticker_link(html)

        return {
            "source": "carfax",
            "carfax_url": url,
            "carfax_title": title,
            "carfax_open_path": None,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "vin": vin,
            "number_of_owners": owners,
            "owner_types": owner_types,
            "owner_type": owner_type,
            "all_service_mercedes_benz": all_service_mercedes_benz,
            "service_facilities": facs,
            "service_record_count": service_record_count,
            "last_reported_odometer": last_reported_odometer,
            "miles_per_year": mpy,
            "low_mileage": low_mileage,
            "no_accidents": no_accidents,
            "no_structural_damage": no_structural_damage,
            "no_total_loss": no_total_loss,
            "warranty_status": warranty_status,
            "warranty_months_remaining": warranty_months_remaining,
            "warranty_miles_remaining": warranty_miles_remaining,
            "titled_states": titled_states,
            "carfax_value": carfax_value,
            "window_sticker_url": sticker_url,
            "window_sticker_source": "carfax_link" if sticker_url else None,
            "raw_text": text,
        }

    # -- options tab (MB CPO fallback when AutoiPacket has no sticker) ----- #

    # Bounds the Selected Packages section: everything from that heading up to
    # (not including) the Find Packages catalog heading.
    _SELECTED_PACKAGES_HEADING_RE = re.compile(r"selected packages", re.IGNORECASE)
    _FIND_PACKAGES_HEADING_RE = re.compile(r"find packages", re.IGNORECASE)

    # Walks <tr> elements in document order between the "Selected Packages"
    # and "Find Packages" headings and returns one {code, name, description}
    # dict per row directly — no separate Python-side grouping pass needed.
    #
    # Live DOM of the LoadPackages.aspx iframe (confirmed 2026-09-20, headless):
    # table#SelectedOptions, one <tr> per package with THREE <td>s — an
    # action cell (empty-text "Remove Option From Vehicle" link), the CODE
    # cell, then an "Ad Text" cell holding <input type=text> (name) and
    # <textarea> (description). The code cell is located by content, not
    # position, and name/description are read from the form controls.
    _SELECTED_PACKAGES_JS = """
    () => {
        function cellText(el) { return (el.textContent || '').replace(/\\s+/g, ' ').trim(); }
        const CODE_RE = /^[A-Z0-9]{2,6}$/;
        const EXCLUDE = new Set([
            'CODE', 'TEXT', 'AD', 'FOR', 'PACKAGE', 'PACKAGES', 'NAME',
            'DESCRIPTION', 'SELECTED', 'FIND', 'STANDARD', 'OPTIONAL',
        ]);

        const all = [...document.querySelectorAll('*')];
        const startEl = all.find(el => el.children.length === 0 && /selected packages/i.test(cellText(el)));
        const endEl = all.find(el => el.children.length === 0 && /find packages/i.test(cellText(el)));
        function isAfter(a, b) {
            return !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
        }

        const rows = [...document.querySelectorAll('tr')].filter(tr => {
            if (startEl && !isAfter(startEl, tr)) return false;
            if (endEl && !isAfter(tr, endEl)) return false;
            return true;
        });

        const out = [];
        for (const tr of rows) {
            const tds = [...tr.querySelectorAll('td')];
            if (tds.length < 2) continue;
            // Find the code cell by content, not fixed position: each row
            // starts with an action cell (a "Remove Option From Vehicle"
            // link with no text) before the CODE column, which shifted every
            // index by one and caused every row to be silently skipped.
            const codeIdx = tds.findIndex(td => {
                const t = cellText(td);
                return CODE_RE.test(t) && !EXCLUDE.has(t.toUpperCase());
            });
            if (codeIdx === -1) continue;
            const code = cellText(tds[codeIdx]);
            // The cell after CODE holds an <input type=text> (short package
            // name) and a <textarea> (long description) — form-control values,
            // which textContent doesn't return for the <input>. Prefer them;
            // fall back to plain cell text for a read-only rendering.
            const adCell = tds.length > codeIdx + 1 ? tds[codeIdx + 1] : null;
            const nameInput = adCell ? adCell.querySelector('input[type=text]') : null;
            const descArea = adCell ? adCell.querySelector('textarea') : null;
            const cellFallback = adCell ? cellText(adCell) : '';
            const name = ((nameInput && nameInput.value) || cellFallback).trim();
            const description = ((descArea && (descArea.value || descArea.textContent)) || '').trim() ||
                (tds.length > codeIdx + 2 ? cellText(tds[codeIdx + 2]) : name);
            out.push({ code: code.toUpperCase(), name, description });
        }
        return out;
    }
    """

    # Same idea for the Find Packages catalog, but we only need CODE ->
    # APPROX ORIG MSRP pairs out of it — never the rest of the catalog. Live
    # rows (confirmed 2026-09-20) are four <td>s: an "add" checkbox cell with no
    # text, the code, the package text, and the price. The code cell is found
    # by content, not position (the checkbox cell is first), and the price is
    # searched for in the whole row's text rather than assuming its column.
    _FIND_PACKAGES_PRICES_JS = """
    () => {
        function cellText(el) { return (el.textContent || '').replace(/\\s+/g, ' ').trim(); }
        const CODE_RE = /^[A-Z0-9]{2,6}$/;
        const PRICE_RE = /\\$[\\d,]+(?:\\.\\d{1,2})?/;

        const all = [...document.querySelectorAll('*')];
        const startEl = all.find(el => el.children.length === 0 && /find packages/i.test(cellText(el)));
        function isAfter(a, b) {
            return !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
        }
        const rows = startEl
            ? [...document.querySelectorAll('tr')].filter(tr => isAfter(startEl, tr))
            : [];

        const out = [];
        for (const tr of rows) {
            const tds = [...tr.querySelectorAll('td')];
            if (!tds.length) continue;
            // Find the code cell by content, not fixed position: each row
            // starts with a checkbox cell (no text) ahead of the code.
            const codeIdx = tds.findIndex(td => CODE_RE.test(cellText(td)));
            if (codeIdx === -1) continue;
            const code = cellText(tds[codeIdx]);
            const m = cellText(tr).match(PRICE_RE);
            if (m) out.push({ code: code.toUpperCase(), price: m[0] });
        }
        return out;
    }
    """

    def scrape_options_tab(self, vehicle_id: str) -> dict[str, Any]:
        """Scrape the ACV Max Options tab for `vehicle_id` — the MB CPO
        fallback when AutoiPacket can't produce a real window sticker for a
        Mercedes-Benz vehicle.

        Reads only the "Selected Packages" section (what this vehicle actually
        has: code, package name, description) and, for any of those codes
        that also appear in the "Find Packages" catalog below it, that
        package's APPROX ORIG MSRP. The catalog itself — every package the
        vehicle does NOT have — is never scraped beyond that price lookup.

        Color (exterior/interior) isn't on this tab; it comes from a second,
        best-effort visit to /inventory/{vehicle_id}/equipment, which never
        raises — a failed color read just leaves both fields None.

        Raises OptionsTabError if the tab never renders a Selected Packages
        section, or nothing could be parsed out of it.
        """
        assert self.page is not None
        url = f"{ACVMAX_INVENTORY_URL}/{vehicle_id}/options"
        self.page.goto(url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass

        # The Options tab is an empty outer shell — real content renders
        # inside its own merchandising iframe (LoadPackages.aspx), exactly
        # like the pricing tab. Reading self.page here (instead of the
        # attached frame) is what previously produced empty results.
        frame = self._attach_merchandising_frame(
            vehicle_id,
            ACVMAX_OPTIONS_FRAME_HINT,
            error_cls=OptionsTabError,
            no_iframe_tag=f"acvmax-no-options-iframe-{vehicle_id}",
            no_frame_tag=f"acvmax-options-frame-{vehicle_id}",
        )

        try:
            frame.get_by_text(self._SELECTED_PACKAGES_HEADING_RE).first.wait_for(
                timeout=self.timeout_ms
            )
        except PlaywrightTimeoutError as exc:
            self._dump_debug(f"acvmax-no-options-{vehicle_id}")
            raise OptionsTabError(
                f"Options tab for vehicle {vehicle_id} never rendered a "
                f"Selected Packages section."
            ) from exc

        # The heading can render before the row data does: wait for at least
        # one table cell to actually attach before reading anything. Headless
        # mode renders this tab as a plain read-only table (<tr>/<td>), not
        # the <input>/<textarea> form an interactive session shows, so "td"
        # is the readiness signal here, not "input".
        try:
            frame.wait_for_selector("table tr td", timeout=15_000)
        except PlaywrightTimeoutError as exc:
            self._dump_debug(f"acvmax-options-no-rows-{vehicle_id}")
            raise OptionsTabError(
                f"Options tab for vehicle {vehicle_id} showed a Selected "
                f"Packages heading but no package rows ever rendered."
            ) from exc

        selected_packages = frame.evaluate(self._SELECTED_PACKAGES_JS)
        if not selected_packages:
            self._dump_debug(f"acvmax-options-empty-{vehicle_id}")
            raise OptionsTabError(
                f"Options tab for vehicle {vehicle_id} loaded but no selected "
                f"packages could be parsed out of it."
            )

        try:
            price_rows = frame.evaluate(self._FIND_PACKAGES_PRICES_JS)
        except Exception:  # noqa: BLE001 - price enrichment is best-effort
            price_rows = []
        price_by_code = self._parse_find_packages_prices(price_rows)
        for pkg in selected_packages:
            code = (pkg.get("code") or "").upper()
            pkg["approx_msrp"] = price_by_code.get(code)

        try:
            exterior_color, interior_color = self._scrape_equipment_colors(vehicle_id)
        except Exception as exc:  # noqa: BLE001 - color is a bonus signal, never fatal here
            print(f"[scraper] options tab: equipment-tab color lookup failed: {exc}")
            exterior_color, interior_color = None, None

        return {
            "source": "acvmax_options_tab",
            "vehicle_id": vehicle_id,
            "selected_packages": selected_packages,
            "exterior_color": exterior_color,
            "interior_color": interior_color,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def _parse_find_packages_prices(cls, rows: list[dict[str, str]]) -> dict[str, float]:
        """{MFR CODE: APPROX ORIG MSRP} from the Find Packages catalog rows
        (see _FIND_PACKAGES_PRICES_JS). Last value wins on a duplicate code."""
        out: dict[str, float] = {}
        for row in rows or []:
            code = (row.get("code") or "").strip().upper()
            price = _money(row.get("price"))
            if code and price is not None:
                out[code] = price
        return out

    def _scrape_equipment_colors(self, vehicle_id: str) -> tuple[str | None, str | None]:
        """Best-effort exterior/interior color read from the Equipment tab —
        the Options tab has no color fields of its own.

        Same empty-outer-shell situation as pricing and options: the real
        content is inside a merchandising iframe (LoadEquipment.aspx), so this
        must read the attached frame, not self.page.
        """
        assert self.page is not None
        url = f"{ACVMAX_INVENTORY_URL}/{vehicle_id}/equipment"
        self.page.goto(url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass

        frame = self._attach_merchandising_frame(
            vehicle_id,
            ACVMAX_EQUIPMENT_FRAME_HINT,
            error_cls=OptionsTabError,
            no_iframe_tag=f"acvmax-no-equipment-iframe-{vehicle_id}",
            no_frame_tag=f"acvmax-equipment-frame-{vehicle_id}",
        )
        text = frame.locator("body").inner_text()
        return self._parse_equipment_colors(frame, text)

    @staticmethod
    def _parse_equipment_colors(target: Any, text: str) -> tuple[str | None, str | None]:
        """Exterior/interior color selectors a few rows below the equipment
        list. Tries the <select>'s chosen option first; falls back to a plain
        text label when the selector markup doesn't match."""

        def _selected_option_text(label_hint: str) -> str | None:
            try:
                loc = target.locator(
                    f"select[aria-label*='{label_hint}' i], "
                    f"select[name*='{label_hint}' i], "
                    f"select[id*='{label_hint}' i]"
                ).first
                if not loc.count():
                    return None
                val = loc.evaluate(
                    "el => el.options[el.selectedIndex] ? "
                    "el.options[el.selectedIndex].text : null"
                )
                return _clean(val) if val else None
            except Exception:  # noqa: BLE001 - best effort, text fallback covers this
                return None

        def _label_then_value(label: str) -> str | None:
            # Same-line label: "Label: Value" or "Label    Value" (adjacent
            # <td> cells in one row typically read this way).
            m = re.search(rf"{re.escape(label)}:?\s*(.+)", text, re.IGNORECASE)
            if m:
                val = _clean(m.group(1))
                if val:
                    return val
            # Label alone on its own line, value on the next non-empty line —
            # the shape a label/value pair takes when each is its own <tr>,
            # which is how this app renders read-only tables in headless mode
            # (see the Options tab fix: label and value routinely land in
            # separate cells/rows here rather than one "Label: value" string).
            lines = [ln.strip() for ln in text.splitlines()]
            for i, ln in enumerate(lines):
                if re.fullmatch(rf"{re.escape(label)}:?", ln, re.IGNORECASE):
                    for nxt in lines[i + 1 :]:
                        if nxt:
                            return _clean(nxt)
            return None

        ext = _selected_option_text("exterior")
        intr = _selected_option_text("interior")

        if not ext:
            ext = _label_then_value("Exterior Color")
        if not intr:
            intr = _label_then_value("Interior Color")

        return ext, intr

    # -- CTR (click-through rate) ------------------------------- #

    def scrape_ctr(
        self,
        vehicle_id: str,
        business_unit_id: str | int | None = None,
    ) -> dict[str, Any]:
        """Scrape the "Price In the Game" CTR graph for the vehicle whose pricing
        page is currently loaded.

        Precondition: self.page is on a /inventory/<id>/pricing page with the
        PricingAnalysis iframe loaded (call scrape_pricing()/open_pricing() first).

        Reads business_unit_id / inventory_id / stock number from the
        #CtrGraphLink element (falling back to the passed args), pulls the CTR
        JSON from InventoryMVCGraph.aspx/CtrPrice, and also opens the graph dialog
        to read the "Average" line from window.Highcharts.charts[0].series.
        """
        assert self.page is not None
        frame = next(
            (f for f in self.page.frames if ACVMAX_PRICING_FRAME_HINT in (f.url or "")),
            None,
        )
        if frame is None:
            self._dump_debug("ctr-no-pricing-frame")
            raise ScraperError(
                "scrape_ctr: no pricing iframe on the current page — call "
                "scrape_pricing()/open_pricing() first."
            )

        # --- identifiers from #CtrGraphLink (args are the fallback) ---
        buid = str(business_unit_id) if business_unit_id is not None else None
        inv = str(vehicle_id) if vehicle_id is not None else None
        stock_number = None
        try:
            link = frame.locator("#CtrGraphLink").first
            link.wait_for(state="attached", timeout=15_000)
            buid = link.get_attribute("data-businessunitid") or buid
            inv = link.get_attribute("data-inventoryid") or inv
            stock_number = link.get_attribute("data-stocknum")
        except PlaywrightTimeoutError:
            pass
        if not buid or not inv:
            self._dump_debug(f"ctr-no-ids-{vehicle_id}")
            raise ScraperError(
                f"scrape_ctr: could not resolve business_unit_id ({buid}) / "
                f"inventory_id ({inv}) from #CtrGraphLink or the passed args."
            )

        ctr_url = (
            f"https://max.firstlook.biz/merchandising/InventoryMVCGraph.aspx"
            f"/CtrPrice/{buid}/{inv}"
        )

        def _classify(name: str) -> str:
            low = (name or "").lower()
            if "autotrader" in low:
                return "autotrader"
            if "cargurus" in low:
                return "cargurus"
            if "average" in low:
                return "average"
            return "other"

        def _norm_date(s: str) -> str | None:
            """'8/3/2026' -> '2026-08-03' (sortable); None if unparseable."""
            try:
                return datetime.strptime(s, "%m/%d/%Y").strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                return None

        def _epoch_to_date(x: Any) -> str | None:
            try:
                ms = float(x)
            except (TypeError, ValueError):
                return None
            if ms > 1e11:      # milliseconds
                secs = ms / 1000
            elif ms > 1e8:     # seconds
                secs = ms
            else:              # an index, not a timestamp
                return None
            try:
                return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                return None

        # {kind -> {YYYY-MM-DD: percent}}. JSON rates are decimals (0.0112);
        # Highcharts yData is already a percent (1.12). Everything is stored as
        # a percent here.
        maps: dict[str, dict[str, float]] = {
            "autotrader": {}, "cargurus": {}, "average": {}
        }
        other_maps: dict[str, dict[str, float]] = {}
        price_events: list[dict[str, Any]] = []

        def _map_for(kind: str, name: str) -> dict[str, float]:
            return maps[kind] if kind in maps else other_maps.setdefault(name, {})

        # --- 1. per-source daily rates from the JSON endpoint (has real dates) ---
        try:
            resp = self.page.context.request.get(ctr_url)
            if resp.ok:
                payload = resp.json()
                price_events = payload.get("PriceEvents") or []
                for name, points in (payload.get("Sources") or {}).items():
                    tgt = _map_for(_classify(name), name)
                    for p in points:
                        d = _norm_date(p.get("Date"))
                        if d is not None:
                            tgt[d] = round(float(p.get("Rate") or 0) * 100, 6)
        except Exception as exc:  # noqa: BLE001
            print(f"[scraper] ctr: JSON endpoint fetch failed ({exc})")

        # --- 2. all series from the rendered Highcharts graph. Its "Average"
        #        line is the only source for that series; AutoTrader/CarGurus
        #        from here fill in / override the JSON. ---
        hc_series: list[dict[str, Any]] = []
        try:
            frame.locator("#CtrGraphLink").first.scroll_into_view_if_needed()
            frame.locator("#CtrGraphLink").first.click(timeout=8_000)
            for _ in range(20):
                hc_series = frame.evaluate(
                    """() => {
                        if (!(window.Highcharts && Highcharts.charts)) return [];
                        const c = Highcharts.charts.filter(Boolean)[0];
                        if (!c) return [];
                        return (c.series || []).map(s => ({
                            name: s.name,
                            x: Array.from(s.xData || []),
                            y: Array.from(s.yData || s.processedYData || []).map(Number),
                        }));
                    }"""
                ) or []
                if hc_series:
                    break
                frame.wait_for_timeout(1_000)
        except (PlaywrightTimeoutError, ScraperError) as exc:
            print(f"[scraper] ctr: could not open the CTR graph dialog ({exc})")
        except Exception as exc:  # noqa: BLE001
            print(f"[scraper] ctr: Highcharts read error ({exc})")

        for s in hc_series:
            tgt = _map_for(_classify(s.get("name") or ""), s.get("name") or "?")
            xs, ys = s.get("x") or [], s.get("y") or []
            for i, y in enumerate(ys):
                if y is None:
                    continue
                d = _epoch_to_date(xs[i]) if i < len(xs) else None
                if d is not None:
                    tgt[d] = round(float(y), 6)

        # --- 3. unified date axis + aligned series ---
        all_dates = sorted(
            {d for m in (*maps.values(), *other_maps.values()) for d in m}
        )
        date_labels = []
        for d in all_dates:
            dt = datetime.strptime(d, "%Y-%m-%d")
            date_labels.append(f"{dt.month}/{dt.day}/{dt.year}")

        def _align(m: dict[str, float]) -> list[float | None]:
            return [m.get(d) for d in all_dates]

        autotrader_series = _align(maps["autotrader"])
        cargurus_series = _align(maps["cargurus"])
        average_series = _align(maps["average"])
        other_series = {k: _align(v) for k, v in other_maps.items()}

        def _latest(series: list[float | None]) -> float | None:
            for v in reversed(series):
                if v:
                    return v
            return next((v for v in reversed(series) if v is not None), None)

        return {
            "source": "acvmax_ctr",
            "ctr_url": ctr_url,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "vehicle_id": inv,
            "business_unit_id": buid,
            "stock_number": stock_number,
            "autotrader_ctr_series": autotrader_series,
            "cargurus_ctr_series": cargurus_series,
            "average_ctr_series": average_series,
            "other_ctr_series": other_series,
            "date_labels": date_labels,
            "price_events": price_events,
            "latest_autotrader_ctr": _latest(autotrader_series),
            "latest_cargurus_ctr": _latest(cargurus_series),
            "latest_average_ctr": _latest(average_series),
        }

    # -- benchmark stores --------------------------------------- #

    def switch_dealership(self, dealership_name: str) -> None:
        """Point ACV MAX at a different store via the secured dealer index (the
        same handoff login() uses), so it works for any store this login can
        reach. Raises VehicleNotFoundError if the store isn't in the account."""
        assert self.page is not None
        self.page.goto(ACVMAX_DEALER_INDEX_URL, wait_until="networkidle")
        link = self.page.locator(f'a:has-text("{dealership_name}")').first
        try:
            link.wait_for(state="visible", timeout=self.timeout_ms)
        except PlaywrightTimeoutError as exc:
            self._dump_debug(f"acvmax-switch-{_slug(dealership_name)}")
            raise VehicleNotFoundError(
                f"Dealership {dealership_name!r} is not in this ACV MAX "
                f"account's store list."
            ) from exc
        try:
            with self.page.expect_navigation(wait_until="networkidle"):
                link.click()
        except PlaywrightTimeoutError:
            pass
        self.page.wait_for_timeout(3_000)
        if "my.max.auto" not in self.page.url:
            self._dump_debug(f"acvmax-switch-{_slug(dealership_name)}")
            raise ScraperError(
                f"Switching to {dealership_name!r} did not hand off to "
                f"my.max.auto (at {self.page.url})."
            )
        print(f"[scraper] switched dealership: {dealership_name}")

    def scrape_benchmark_inventory(
        self, dealership_name: str
    ) -> list[dict[str, Any]]:
        """Switch ACV MAX to `dealership_name`, crawl its retail inventory (same
        pagination as inventory_crawler.crawl_inventory), scrape CTR per vehicle,
        then switch back to Mercedes-Benz of Durham. Durham is always restored,
        even if the crawl raises.

        Returns one dict per vehicle with a CTR read: stock_number, vin,
        year_make_model, mileage, current_price, certified, days_on_lot,
        ctr_data.
        """
        assert self.page is not None
        # Lazy import: inventory_crawler imports this module at load time.
        from inventory_crawler import (
            _PAGE_SAFETY_LIMIT,
            _ROW_JS,
            _next_page,
            _normalize_row,
            _total_count,
            _wait_for_rows,
        )

        page = self.page
        out: list[dict[str, Any]] = []
        try:
            self.switch_dealership(dealership_name)
            page.goto(ACVMAX_INVENTORY_URL, wait_until="domcontentloaded")
            _wait_for_rows(page)

            raw_rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            total = _total_count(page)
            pages = 0
            while pages < _PAGE_SAFETY_LIMIT:
                _wait_for_rows(page)
                for raw in page.evaluate(_ROW_JS) or []:
                    vid = raw.get("vehicle_id")
                    if vid and vid not in seen:
                        seen.add(vid)
                        raw_rows.append(raw)
                pages += 1
                print(
                    f"[scraper] {dealership_name}: page {pages}, "
                    f"{len(raw_rows)} vehicle(s)"
                    + (f" / {total}" if total else "")
                )
                if total and len(raw_rows) >= total:
                    break
                if not _next_page(page):
                    break

            vehicles = [_normalize_row(r) for r in raw_rows]
            # Benchmark stores don't share Durham's status-code map — "retail" is
            # just the RETAIL objective here; the tier comes from infer_tier().
            retail = [
                v
                for v in vehicles
                if (v.get("objective") or "").upper() == "RETAIL"
            ]
            print(
                f"[scraper] {dealership_name}: {len(retail)} retail of "
                f"{len(vehicles)} crawled"
            )

            for v in retail:
                vid = v.get("vehicle_id")
                if not vid:
                    continue
                try:
                    self.open_pricing(vid)
                    ctr = self.scrape_ctr(vid)
                except ScraperError as exc:
                    print(
                        f"[scraper] {dealership_name}: no CTR for "
                        f"{v.get('stock_number')} — {exc}"
                    )
                    continue
                out.append(
                    {
                        "stock_number": v.get("stock_number"),
                        "vin": v.get("vin"),
                        "year_make_model": v.get("year_make_model"),
                        "mileage": v.get("mileage"),
                        "current_price": v.get("current_price"),
                        "certified": v.get("certified"),
                        "days_on_lot": v.get("days_on_lot"),
                        "ctr_data": ctr,
                    }
                )
            return out
        finally:
            try:
                self.switch_dealership(ACVMAX_DEALERSHIP)
            except ScraperError as exc:
                print(
                    f"[scraper] WARNING: could not switch back to "
                    f"{ACVMAX_DEALERSHIP} — {exc}",
                    file=sys.stderr,
                )

    # -- convenience --------------------------------------------- #

    def scrape(self, stock_number: str) -> dict[str, Any]:
        self.login()
        return self.scrape_pricing(stock_number)


def _fmt(n: float | None) -> str:
    if n is None:
        return "?"
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.2f}"


def _hours(text: str | None) -> float | None:
    """Parse '1.5' / '1.5 hrs' / '1:30' -> 1.5 ; None on failure."""
    if not text:
        return None
    t = text.strip()
    m = re.match(r"^(\d+):([0-5]\d)$", t)
    if m:
        return round(int(m.group(1)) + int(m.group(2)) / 60, 2)
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None


# --------------------------------------------------------------------------- #
# ReconVision scraper
# --------------------------------------------------------------------------- #

# DOM walker for the work-order line items. Verified against the live
# /work_orders/<id>/edit page: each department is a
# `section.work-order-breakdown--department.work-order-<slug>`; each line item is
# a `tr.table__row--content[data-service-id]`. Description / hours / labor live in
# <input> values; parts / total / status live in cell text. Returns raw strings;
# Python parses the numbers.
_RV_LINE_ITEM_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const titleCase = s => s.replace(/[-_]+/g, ' ')
                          .replace(/\b\w/g, c => c.toUpperCase()).trim();
  const secs = [...document.querySelectorAll('.work-order-breakdown--department')];
  const out = [];
  for (const sec of secs) {
    // The real display name is the section text before the "DMS" column header.
    let dept = norm(sec.innerText).split(/\s+DMS\s+/)[0];
    if (!dept || dept.length > 60) {
      const tok = (sec.className || '').split(/\s+/)
        .find(c => /^work-order-[a-z]/.test(c) && c !== 'work-order-breakdown--department');
      dept = tok ? titleCase(tok.replace(/^work-order-/, '')).replace(/\bRo\b/g, 'RO') : '';
    }

    let deptTotal = null;
    for (const e of sec.querySelectorAll('*')) {
      if (e.children.length <= 3 && /dept\.?\s*total/i.test(e.textContent || '')) {
        const m = (e.parentElement?.innerText || e.innerText || '').match(/\$[\d,]+\.\d{2}/);
        if (m) { deptTotal = m[0]; break; }
      }
    }

    for (const r of sec.querySelectorAll('tr.table__row--content')) {
      const q = sel => r.querySelector(sel);
      const nameEl = q("input[name*='[name]']");
      const hoursEl = q("input.labor-hours, input[name*='[hours]']");
      const laborEl = q(".work-order-cell--labor input, input.labor-rate, input[name*='[rate]']");
      const partsCell = q(".work-order-cell--parts");
      const totalCell = q(".work-order-cell--total");
      const actCell = q(".work-order-cell--act");
      const rejectedEl = q("input[name*='[rejected]'], input.line-item-approved");
      const isTask = !!q(".work-order-cell--task-only");
      const tipEl = q("[data-title*='Operation Code'], [data-title*='Repair Order']");
      const tip = tipEl ? (tipEl.getAttribute('data-title') || '') : '';
      const ro = tip.match(/Repair Order #:\s*(\S+)/i);
      const op = tip.match(/Operation Code:\s*(\S+)/i);

      out.push({
        service_id: r.getAttribute('data-service-id'),
        section: dept,
        section_total_raw: deptTotal,
        kind: isTask ? 'task' : 'service',
        description: nameEl
          ? (nameEl.value || nameEl.getAttribute('title') || '')
          : norm(r.querySelector('.work-order-cell--accept')?.innerText),
        labor_hours_raw: hoursEl ? hoursEl.value : null,
        labor_cost_raw: laborEl ? laborEl.value : null,
        parts_cost_raw: partsCell ? norm(partsCell.innerText) : null,
        total_cost_raw: totalCell ? norm(totalCell.innerText) : null,
        completion_status: actCell ? norm(actCell.innerText) : null,
        rejected: rejectedEl ? rejectedEl.value === '1' : null,
        repair_order: ro ? ro[1] : null,
        operation_code: op ? op[1] : null,
      });
    }
  }
  return out;
}
"""


class ReconVisionScraper(_BrowserSession):
    """Log into ReconVision (two-step), open a work order by stock number, and
    scrape its line items.

    Usage:
        with ReconVisionScraper(headless=True) as s:
            data = s.scrape("DT23358A")

    Verified end-to-end against the live site (two-step sign-in, stock-number
    search, work-order navigation, line-item extraction). Tunable selectors live
    in SELECTORS under the "rv_*" keys; the line-item DOM walker is _RV_LINE_ITEM_JS.
    """

    SESSION_FILENAME = "reconvision_session.json"

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("viewport", {"width": 1680, "height": 1200})
        super().__init__(**kwargs)

    # -- auth ----------------------------------------------------------- #

    def is_logged_in(self) -> bool:
        assert self.page is not None
        self.page.goto(RECONVISION_HOME_URL, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        self.page.wait_for_timeout(1_500)
        if "sign_in" in self.page.url:
            return False
        if _exists(self.page, "rv_signin_form_marker", timeout_ms=2_000):
            return False
        return _exists(self.page, "rv_home_marker", timeout_ms=8_000)

    def login(self, *, force: bool = False) -> None:
        assert self.page is not None

        if not force and self.use_saved_session and self.is_logged_in():
            print("[scraper] reusing saved ReconVision session")
            return

        username = getattr(credentials, "RECONVISION_USERNAME", "")
        password = getattr(credentials, "RECONVISION_PASSWORD", "")
        if not username or password in ("", "changeme", "your_password_here"):
            raise LoginError(
                "RECONVISION_USERNAME / RECONVISION_PASSWORD are not set in credentials.py"
            )

        print(f"[scraper] ReconVision login (step 1: username) at {RECONVISION_SIGNIN_URL}")
        self.page.goto(RECONVISION_SIGNIN_URL, wait_until="domcontentloaded")
        try:
            _first_visible(self.page, "rv_username", self.timeout_ms).fill(username)
            _first_visible(self.page, "rv_next", self.timeout_ms).click()
        except ScraperError:
            self._dump_debug("reconvision-login-step1")
            raise
        self.page.wait_for_timeout(2_500)

        body = self.page.locator("body").inner_text().lower()
        if "user not found" in body or "username" in body and "not found" in body:
            self._dump_debug("reconvision-username-rejected")
            raise LoginError(
                f"ReconVision rejected the username {username!r} ('User not found')."
            )

        print("[scraper] ReconVision login (step 2: password)")
        try:
            _first_visible(self.page, "rv_password", self.timeout_ms).fill(password)
        except ScraperError:
            self._dump_debug("reconvision-no-password-step")
            raise LoginError(
                "Password field never appeared after clicking Next. Check the "
                "rv_password / rv_next selectors."
            )
        try:
            with self.page.expect_navigation(wait_until="domcontentloaded"):
                _first_visible(self.page, "rv_signin", self.timeout_ms).click()
        except (ScraperError, PlaywrightTimeoutError):
            # Some Devise setups swap via AJAX rather than navigating.
            pass

        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        self.page.wait_for_timeout(1_000)
        if "sign_in" in self.page.url or _exists(
            self.page, "rv_signin_form_marker", timeout_ms=3_000
        ):
            self._dump_debug("reconvision-login-failed")
            raise LoginError(
                "ReconVision sign-in did not complete — still on the sign-in form. "
                "Check RECONVISION_PASSWORD in credentials.py."
            )
        print("[scraper] ReconVision login OK")
        self._save_session()

    # -- search ------------------------------------------------------- #

    @staticmethod
    def _classify_wo_status(row_text: str) -> str:
        """Bucket a search-result row's status label into
        'active' | 'archived' | 'deleted' | 'unknown'."""
        t = (row_text or "").lower()
        if re.search(r"\b(deleted|voided|cancell?ed)\b", t):
            return "deleted"
        if re.search(r"\b(active|in[\s-]?progress|reopened)\b", t):
            return "active"
        if re.search(r"\b(archived|closed|completed)\b", t):
            return "archived"
        return "unknown"

    def find_work_order(self, stock_number: str) -> str | None:
        """Search by stock number and resolve to a single work-order id (digits
        in /work_orders/<id>).

        Raises WorkOrderNotFoundError only when the search returns nothing.
        When several work orders match the same stock number, one is chosen by:
          1. an "Active" RO if present,
          2. otherwise the most recent "Archived" RO (highest work-order number),
          3. otherwise the most recent non-deleted RO of unknown status.
        "Deleted" ROs are never selected. If *every* match is deleted, returns
        None (the caller yields an empty recon result)."""
        assert self.page is not None
        stock = stock_number.strip().upper()
        print(f"[scraper] searching ReconVision for stock #{stock}")

        self.page.goto(RECONVISION_HOME_URL, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        _first_visible(self.page, "rv_home_marker", self.timeout_ms)

        search = _first_visible(self.page, "rv_search", self.timeout_ms)
        search.click()
        search.fill("")
        search.type(stock, delay=40)
        self.page.wait_for_timeout(2_500)
        search.press("Enter")
        self.page.wait_for_timeout(2_500)
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass

        rows = self.page.evaluate(
            r"""() => [...document.querySelectorAll('a[href]')]
                .map(a => ({
                    href: a.getAttribute('href'),
                    row: (a.closest('tr,li,[class*=row],[class*=result],[class*=card]')
                          || a.parentElement)?.innerText || a.innerText || ''
                }))
                .filter(x => /\/work_orders\/\d+(\/|$|\?)/.test(x.href || ''))"""
        )
        by_id: dict[str, str] = {}
        for r in rows:
            m = re.search(r"/work_orders/(\d+)", r["href"] or "")
            if not m:
                continue
            wid = m.group(1)
            by_id[wid] = max(by_id.get(wid, ""), r["row"], key=len)

        stock_bare = stock.lstrip("#")
        matches = [
            wid for wid, txt in by_id.items()
            if stock_bare in txt.upper().replace("#", "")
        ] or list(by_id)

        if not matches:
            self._dump_debug(f"reconvision-search-{stock_bare}")
            raise WorkOrderNotFoundError(
                f"Stock #{stock_bare}: no work order found. "
                f"See {DEBUG_DIR}/ for a snapshot."
            )

        if len(matches) == 1:
            wid = matches[0]
            print(f"[scraper] matched work order {wid}")
            return wid

        # More than one work order for this stock — resolve by status priority.
        buckets: dict[str, list[str]] = {
            "active": [], "archived": [], "deleted": [], "unknown": []
        }
        for wid in matches:
            buckets[self._classify_wo_status(by_id.get(wid, ""))].append(wid)

        summary = ", ".join(
            f"{wid}={self._classify_wo_status(by_id.get(wid, ''))}" for wid in matches
        )
        for tier in ("active", "archived", "unknown"):
            if buckets[tier]:
                wid = max(buckets[tier], key=lambda w: int(w))
                print(
                    f"[scraper] {len(matches)} work orders for stock #{stock_bare} "
                    f"({summary}) — selected {tier} work order {wid}"
                )
                return wid

        # Every match is a deleted/voided RO.
        self._dump_debug(f"reconvision-search-{stock_bare}")
        print(
            f"[scraper] WARNING: stock #{stock_bare} has only deleted work orders "
            f"({summary}) — returning an empty recon result",
            file=sys.stderr,
        )
        return None

    # -- scrape ----------------------------------------------------- #

    def _empty_work_order(self, stock_number: str, note: str) -> dict[str, Any]:
        """Result shape for stocks with no usable work order (only deleted ROs).
        line_items is empty so the recon gate treats recon as 'not blocking'."""
        return {
            "source": "reconvision",
            "source_url": None,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "stock_number": stock_number.strip().lstrip("#"),
            "work_order_id": None,
            "inventory_number": None,
            "repair_order_number": None,
            "vin": None,
            "vehicle": None,
            "page_title": None,
            "sections": [],
            "section_totals": {},
            "line_item_count": 0,
            "line_items": [],
            "note": note,
            "raw_text": "",
            "recon_image_bytes": None,
            "recon_image_path": None,
        }

    def scrape_work_order(self, stock_number: str) -> dict[str, Any]:
        assert self.page is not None
        wo_id = self.find_work_order(stock_number)
        if wo_id is None:
            return self._empty_work_order(
                stock_number,
                "Only deleted/voided work orders exist for this stock number.",
            )
        url = f"{RECONVISION_HOME_URL}work_orders/{wo_id}/edit"
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            raise WorkOrderLoadError(
                f"Work order {wo_id} (stock #{stock_number.strip().lstrip('#')}): "
                f"the edit page timed out loading. {exc}"
            ) from exc
        try:
            self.page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        self.page.wait_for_timeout(2_000)

        raw_items = self.page.evaluate(_RV_LINE_ITEM_JS) or []
        line_items = [self._normalize_line_item(it) for it in raw_items]
        text = self.page.locator("body").inner_text()

        if not line_items:
            self._dump_debug(f"reconvision-lineitems-{wo_id}")

        sections: list[str] = []
        section_totals: dict[str, float | None] = {}
        for it, raw in zip(line_items, raw_items):
            sec = it["section"]
            if sec and sec not in sections:
                sections.append(sec)
                section_totals[sec] = _money(raw.get("section_total_raw"))

        def _find(label: str) -> str | None:
            m = re.search(rf"{label}\s*[#:]?\s*(.+)", text)
            return _clean(m.group(1)) if m else None

        vin_m = re.search(r"\bVIN\b[:\s]*(" + _VIN_RE.pattern + r")", text)
        ymm_m = re.search(r"\n(\d{4}\s+[A-Z][A-Z0-9 &/-]+)\n", text)
        vin = vin_m.group(1) if vin_m else None

        image_bytes, image_path = self._capture_recon_screenshot(self.page, vin)

        return {
            "source": "reconvision",
            "source_url": url,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "stock_number": stock_number.strip().lstrip("#"),
            "work_order_id": wo_id,
            "inventory_number": _find("Inv"),
            "repair_order_number": _find("RO"),
            "vin": vin,
            "vehicle": _clean(ymm_m.group(1)) if ymm_m else None,
            "page_title": self.page.title(),
            "sections": sections,
            "section_totals": section_totals,
            "line_item_count": len(line_items),
            "line_items": line_items,
            "raw_text": text,
            # image_bytes is for this-call, same-process use only (e.g. an
            # immediate vision-parse) — it is NOT included in what gets
            # json.dumps()'d into vehicle_cache.db (see vehicle_cache.save_recon()),
            # only recon_image_path is. Re-load from that path (see
            # vision_parser._load_image_bytes()) on any later cache hit.
            "recon_image_bytes": image_bytes,
            "recon_image_path": image_path,
        }

    @staticmethod
    def _capture_recon_screenshot(page: Page, vin: str | None) -> tuple[bytes | None, str | None]:
        """Full-page PNG of the open work-order edit page, saved to
        recon_cache/<vin>.png for vision_parser.parse_recon_image(). Returns
        (image_bytes, path), both None on any capture failure or a missing
        VIN — this is a bonus data source for the vision parser, never
        something the ReconVision scrape itself should fail over.
        full_page=True matters here: a work order's line items scroll well
        past the fold, same reasoning as _capture_carfax_screenshot()."""
        if not vin:
            return None, None
        path = RECON_CACHE_DIR / f"{vin}.png"
        try:
            image_bytes = page.screenshot(path=str(path), type="png", full_page=True)
        except Exception as exc:  # noqa: BLE001 - best-effort only
            print(f"[scraper] recon screenshot capture failed for {vin}: {exc}")
            return None, None
        return image_bytes, str(path)

    @staticmethod
    def _normalize_line_item(it: dict[str, Any]) -> dict[str, Any]:
        status = _clean(it.get("completion_status")) or None
        # Observed --act cell values: "TASK COMPLETED" (done), "COMPLETE TASK"
        # (a call-to-action button, i.e. NOT done), "INCOMPLETE" (not done).
        norm_status = (status or "").strip().upper()
        completed = bool(re.fullmatch(r"(TASK\s+)?COMPLETED?|DONE", norm_status))
        return {
            "service_id": it.get("service_id"),
            "section": it.get("section"),
            "kind": it.get("kind"),
            "description": _clean(it.get("description")) or None,
            "labor_hours": _hours(it.get("labor_hours_raw")),
            "labor_cost": _money(it.get("labor_cost_raw")),
            "parts_cost": _money(it.get("parts_cost_raw")),
            "total_cost": _money(it.get("total_cost_raw")),
            "completion_status": status,
            "completed": completed,
            "rejected": it.get("rejected"),
            "repair_order": it.get("repair_order"),
            "operation_code": it.get("operation_code"),
        }

    # -- convenience --------------------------------------------- #

    def scrape(self, stock_number: str) -> dict[str, Any]:
        self.login()
        return self.scrape_work_order(stock_number)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ad writer scrapers")
    parser.add_argument(
        "identifier",
        nargs="?",
        help="VIN (autoipacket) or stock number (acvmax / reconvision); "
        "omit for --source ipacket_browse",
    )
    parser.add_argument(
        "--source",
        choices=("autoipacket", "acvmax", "reconvision", "ipacket_browse"),
        default="autoipacket",
        help="which scraper to run (default: autoipacket). ipacket_browse builds "
        "rarity.db from the packets inventory list.",
    )
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--slow-mo", type=int, default=0, help="ms delay between actions")
    parser.add_argument(
        "--fresh-login", action="store_true", help="ignore any saved session"
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="leave the (headed) browser open after scraping for inspection",
    )
    parser.add_argument(
        "--carfax",
        action="store_true",
        help="(acvmax only) also open and scrape the Carfax report",
    )
    parser.add_argument(
        "--location",
        default=None,
        help="(ipacket_browse only) restrict the crawl to this store's cards, "
        "e.g. --location 'Mercedes-Benz of Durham'",
    )
    args = parser.parse_args(argv)

    if args.source == "ipacket_browse":
        return crawl_ipacket_inventory(
            headless=not args.headed,
            fresh_login=args.fresh_login,
            location_filter=args.location,
        )

    if not args.identifier:
        parser.error("an identifier (VIN or stock number) is required")

    dispatch = {
        "autoipacket": (AutoiPacketScraper, "pull_sticker", "raw_text"),
        "acvmax": (ACVMaxScraper, "scrape_pricing", "raw_pricing_text"),
        "reconvision": (ReconVisionScraper, "scrape_work_order", "raw_text"),
    }
    cls, method, raw_key = dispatch[args.source]

    try:
        with cls(
            headless=not args.headed,
            slow_mo_ms=args.slow_mo,
            use_saved_session=not args.fresh_login,
            keep_open=args.keep_open,
        ) as scraper:
            scraper.login(force=args.fresh_login)
            data = getattr(scraper, method)(args.identifier)
            if args.carfax and args.source == "acvmax":
                data["carfax"] = scraper.scrape_carfax(
                    acvmax_mileage=data.get("mileage")
                )
    except ScraperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    printable = {k: v for k, v in data.items() if k not in (raw_key, "recon_image_bytes")}
    if isinstance(printable.get("carfax"), dict):
        printable["carfax"] = {
            k: v for k, v in printable["carfax"].items() if k != "raw_text"
        }
    print(json.dumps(printable, indent=2))
    print(f"\n[{raw_key}: {len(data.get(raw_key, ''))} chars omitted above]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
