#!/usr/bin/env python3
"""verifier.py — did the ad actually make it onto HendrickCars.com, and is it current?

Homenet syndicates the ad copy we generate into the "dealer notes" block on each
vehicle's VDP at hendrickcars.com. This module:

  * check_hendrickcars(stock)     — scrape the live VDP description (no login)
  * compare_ad(stored, live)      — fuzzy match stored copy vs what's live
  * run_verification(ad_history)  — check every vehicle that's due, update history
  * send_verification_alert(...)  — email the vehicles that need posting / updating

CLI:
    python3 verifier.py P82965            # check one stock against ad_history.json
    python3 verifier.py --all             # run_verification over the whole history
    python3 verifier.py --all --no-email  # ... but don't send the alert
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from run_lock import (
    ORCHESTRATOR_LOCK_PATH,
    ScraperBusyError,
    acquire_scraper_lock,
    release_lock_if_owned,
)
from scraper import _BrowserSession

HENDRICKCARS_HOME = "https://www.hendrickcars.com"
# Dealer.com SRP: a free-text ?search= narrows the inventory grid to the stock #.
_SEARCH_PATHS = (
    "/used-inventory/index.htm?search={q}",
    "/new-inventory/index.htm?search={q}",
)
# The syndicated copy lands in the ws-dealernotes widget on the VDP.
_DESCRIPTION_SELECTORS = (
    "#dealernotes1-app-root .content",
    "#dealernotes1-app-root",
    ".ws-dealernotes .content",
    ".ws-dealernotes",
    '[data-widget-name="ws-dealernotes"]',
)
_VDP_HREF_RE = re.compile(
    r"/(?:used|new|certified|hendrick-certified|hendrick-affordable)/[^\s\"']+\.htm",
    re.IGNORECASE,
)
_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")

VERIFY_INTERVAL_DAYS = 3


# --------------------------------------------------------------------------- #
# 1. Scraper
# --------------------------------------------------------------------------- #


class HendrickCarsScraper(_BrowserSession):
    """Public-site scraper: no login, no saved session."""

    SESSION_FILENAME = "hendrickcars_session.json"

    def __init__(self, *, headless: bool = True, **kw: Any) -> None:
        kw.setdefault("use_saved_session", False)
        super().__init__(headless=headless, **kw)

    # -- helpers ---------------------------------------------------------- #

    def _collect_vdp_links(self) -> list[str]:
        hrefs = self.page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.getAttribute('href'))"
        )
        seen: list[str] = []
        for h in hrefs or []:
            if not h:
                continue
            m = _VDP_HREF_RE.search(h)
            if not m:
                continue
            url = m.group(0).split("#")[0]
            if url not in seen:
                seen.append(url)
        return seen

    def _read_description(self) -> str:
        # nudge lazy widgets into rendering
        try:
            for _ in range(8):
                self.page.mouse.wheel(0, 1400)
                self.page.wait_for_timeout(250)
        except Exception:  # noqa: BLE001
            pass
        for sel in _DESCRIPTION_SELECTORS:
            try:
                self.page.wait_for_selector(sel, timeout=6000, state="attached")
            except Exception:  # noqa: BLE001
                continue
            try:
                txt = (self.page.inner_text(sel) or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if len(txt) >= 40:
                return re.sub(r"\n{3,}", "\n\n", txt)
        return ""

    def _search_vdp_links(self, stock: str) -> list[str]:
        """Search the used then new inventory grids for `stock` and return the
        VDP hrefs found (empty list when neither shows a match)."""
        vdp_links: list[str] = []
        for path in _SEARCH_PATHS:
            self.page.goto(
                HENDRICKCARS_HOME + path.format(q=stock),
                wait_until="domcontentloaded",
            )
            try:
                self.page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            self.page.wait_for_timeout(1200)
            vdp_links = self._collect_vdp_links()
            body = ""
            try:
                body = self.page.inner_text("body")
            except Exception:  # noqa: BLE001
                pass
            if re.search(r"\b0 (?:matches|results|vehicles)\b|no (?:matching )?vehicles", body, re.IGNORECASE):
                vdp_links = []
            if vdp_links:
                break
        return vdp_links

    # -- public -------------------------------------------------------- #

    def find_url(self, stock_number: str) -> str | None:
        """Just the VDP URL for a stock number, or None when it isn't listed
        (yet). Skips check()'s VDP visit and description read, so a caller that
        only wants the link can reuse one session across a whole batch cheaply.
        Raises on a browser/navigation failure so callers can tell "lookup
        failed" apart from "not listed"."""
        stock = stock_number.strip().lstrip("#").upper()
        vdp_links = self._search_vdp_links(stock)
        return HENDRICKCARS_HOME + vdp_links[0] if vdp_links else None

    def check(self, stock_number: str) -> dict[str, Any]:
        stock = stock_number.strip().lstrip("#").upper()
        scraped_at = datetime.now(timezone.utc).isoformat()
        result: dict[str, Any] = {
            "stock_number": stock,
            "url_found": None,
            "page_found": False,
            "description_text": "",
            "scraped_at": scraped_at,
        }

        try:
            vdp_links = self._search_vdp_links(stock)

            if not vdp_links:
                self._dump_debug(f"hendrickcars-no-vdp-{stock}")
                return result

            vdp_url = HENDRICKCARS_HOME + vdp_links[0]
            result["url_found"] = vdp_url
            self.page.goto(vdp_url, wait_until="domcontentloaded")
            try:
                self.page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            self.page.wait_for_timeout(1500)

            result["page_found"] = True
            result["description_text"] = self._read_description()
            if not result["description_text"]:
                self._dump_debug(f"hendrickcars-no-description-{stock}")
            return result
        except Exception as exc:  # noqa: BLE001 - never fatal, just report
            self._dump_debug(f"hendrickcars-error-{stock}")
            result["error"] = str(exc)
            return result


def check_hendrickcars(stock_number: str, *, headless: bool = True) -> dict[str, Any]:
    """Look the vehicle up on hendrickcars.com and return
    {stock_number, url_found, page_found, description_text, scraped_at}."""
    with HendrickCarsScraper(headless=headless) as s:
        return s.check(stock_number)


# --------------------------------------------------------------------------- #
# 2. Comparison logic
# --------------------------------------------------------------------------- #

_GENERIC_RE = re.compile(
    r"^\s*(call|contact|please call|for (?:more )?(?:info|information|details)|"
    r"inquire|visit us|see dealer|coming soon|call for (?:price|details))",
    re.IGNORECASE,
)


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def _first_sentence(paragraph: str) -> str:
    p = re.sub(r"\s+", " ", (paragraph or "").strip())
    m = re.search(r"^(.{25,}?[.!?])(?:\s|$)", p)
    return (m.group(1) if m else p[:140]).strip()


def _extract_key_phrases(stored_ad_text: str) -> list[dict[str, str]]:
    """Pull 5-6 distinctive phrases we expect to survive syndication verbatim."""
    text = stored_ad_text or ""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    p1 = paras[0] if paras else text
    p2 = paras[1] if len(paras) > 1 else ""

    phrases: list[dict[str, str]] = []

    m = _VIN_RE.search(text)
    if m:
        phrases.append({"label": "VIN", "kind": "exact", "phrase": m.group(0)})

    # proof-point dollar figure, e.g. "$4,000 below J.D. Power"
    m = re.search(r"\$[\d,]{3,}(?=\s+(?:below|under|less))", p2) or re.search(
        r"\$[\d,]{3,}", p2
    )
    if m:
        phrases.append({"label": "proof point $", "kind": "exact", "phrase": m.group(0)})

    m = re.search(r"([\d,]{3,})\s+miles", text, re.IGNORECASE)
    if m:
        phrases.append(
            {"label": "mileage", "kind": "exact", "phrase": f"{m.group(1)} miles"}
        )

    # one package name, e.g. "AMG Line Exterior Package"
    pkgs = [
        re.sub(r"^(?:The|A|An)\s+", "", p)
        for p in re.findall(
            r"([A-Z][A-Za-z0-9./-]+(?:[ -][A-Z0-9][A-Za-z0-9./-]+){0,4}\s+Package)",
            text,
        )
    ]
    for pkg in list(dict.fromkeys(pkgs))[:1]:
        phrases.append({"label": f"package: {pkg}", "kind": "exact", "phrase": pkg})

    fs = _first_sentence(p1)
    if fs:
        phrases.append(
            {"label": "paragraph one opener", "kind": "fuzzy", "phrase": fs}
        )

    # top up toward 5-6 phrases with the year/model token and a 2nd package
    if len(phrases) < 5:
        m = re.search(
            r"\b((?:19|20)\d{2})\s+([A-Z][A-Za-z-]+(?:\s+[A-Z0-9][A-Za-z0-9-]+){0,3})",
            text,
        )
        if m:
            phrases.append(
                {"label": "year / model", "kind": "exact", "phrase": m.group(0)}
            )
    if len(phrases) < 5:
        for pkg in list(dict.fromkeys(pkgs))[1:2]:
            phrases.append(
                {"label": f"package: {pkg}", "kind": "exact", "phrase": pkg}
            )

    return phrases


def _phrase_present(phrase: dict[str, str], live_norm: str) -> bool:
    target = _normalize(phrase["phrase"])
    if not target:
        return False
    if phrase["kind"] == "exact":
        return target in live_norm
    # fuzzy: strong token overlap or a high difflib ratio against a window
    words = [w for w in target.split() if len(w) > 3]
    if words:
        hit = sum(1 for w in words if w in live_norm) / len(words)
        if hit >= 0.6:
            return True
    return SequenceMatcher(None, target, live_norm).ratio() >= 0.35


def compare_ad(stored_ad_text: str, live_description_text: str | None) -> dict[str, Any]:
    """Fuzzy-compare the stored ad against the live VDP description.

    Returns {match_score (0-100), matched_phrases, missing_phrases, verdict}.
    verdict: "current" | "outdated" | "not_posted" | "not_found".
    """
    phrases = _extract_key_phrases(stored_ad_text)
    labels = [p["label"] for p in phrases]

    if live_description_text is None:
        return {
            "match_score": 0,
            "matched_phrases": [],
            "missing_phrases": labels,
            "verdict": "not_found",
        }

    live = live_description_text.strip()
    if len(live) < 80 or _GENERIC_RE.match(live):
        return {
            "match_score": 0,
            "matched_phrases": [],
            "missing_phrases": labels,
            "verdict": "not_posted",
        }

    live_norm = _normalize(live)
    matched, missing = [], []
    for p in phrases:
        (matched if _phrase_present(p, live_norm) else missing).append(p["label"])

    score = round(100 * len(matched) / len(phrases)) if phrases else 0
    verdict = "current" if score > 80 else "outdated"
    return {
        "match_score": score,
        "matched_phrases": matched,
        "missing_phrases": missing,
        "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
# 3. Verification runner
# --------------------------------------------------------------------------- #


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _verification_due(entry: dict[str, Any], today: date) -> bool:
    if not entry.get("current_ad_text"):
        return False
    first = _as_date(entry.get("first_ad_date"))
    last_ad = _as_date(entry.get("last_ad_date")) or first
    last_verified = _as_date(entry.get("last_verified"))

    # A: initial ad is >3 days old and we haven't verified in the last 3 days
    if first and (today - first).days > VERIFY_INTERVAL_DAYS:
        if last_verified is None or (today - last_verified).days >= VERIFY_INTERVAL_DAYS:
            return True

    # B: a reprice landed >3 days ago and hasn't been verified since
    if entry.get("lifecycle_stage") == "repriced" and last_ad:
        if (today - last_ad).days > VERIFY_INTERVAL_DAYS and (
            last_verified is None or last_verified < last_ad
        ):
            return True

    return False


def _ymm_from_entry(entry: dict[str, Any]) -> str:
    text = entry.get("paragraph_one") or entry.get("current_ad_text") or ""
    m = re.search(
        r"\b((?:19|20)\d{2})\s+([A-Z][A-Za-z-]+(?:\s+[A-Z0-9][A-Za-z0-9-]+){0,4})",
        text,
    )
    return m.group(0).strip() if m else "unknown vehicle"


def _days_since(entry: dict[str, Any], today: date) -> int | None:
    d = _as_date(entry.get("first_ad_date"))
    return (today - d).days if d else None


def run_verification(
    ad_history: dict[str, Any] | None = None, *, headless: bool = True
) -> tuple[list[dict], list[dict], list[dict]]:
    """Verify every vehicle that's due, update ad_history.json in place, and
    return (current, needs_posting, needs_update).

      current       — verdict "current", no action
      needs_posting — verdict "not_posted" or "not_found"
      needs_update  — verdict "outdated"
    """
    from adwriter import load_ad_history, save_ad_history

    history = ad_history if ad_history is not None else load_ad_history()
    today = date.today()
    current: list[dict] = []
    needs_posting: list[dict] = []
    needs_update: list[dict] = []

    for stock, entry in history.items():
        if not _verification_due(entry, today):
            continue

        print(f"[verify] {stock}: due — checking hendrickcars.com ...")
        try:
            live = check_hendrickcars(stock, headless=headless)
        except Exception as exc:  # noqa: BLE001
            print(f"[verify] {stock}: scrape failed — {exc}", file=sys.stderr)
            continue

        live_text = live["description_text"] if live.get("page_found") else None
        cmp = compare_ad(entry.get("current_ad_text", ""), live_text)

        entry["last_verified"] = today.isoformat()
        entry["verification_verdict"] = cmp["verdict"]
        entry["match_score"] = cmp["match_score"]

        row = {
            "stock_number": stock,
            "year_make_model": _ymm_from_entry(entry),
            "entry": entry,
            "check": live,
            "compare": cmp,
            "days_since_ad": _days_since(entry, today),
        }
        print(
            f"[verify] {stock}: {cmp['verdict']} (score {cmp['match_score']}, "
            f"url {live.get('url_found') or 'n/a'})"
        )
        if cmp["verdict"] == "current":
            current.append(row)
        elif cmp["verdict"] == "outdated":
            needs_update.append(row)
        else:  # not_posted / not_found
            needs_posting.append(row)

    save_ad_history(history)
    return current, needs_posting, needs_update


# --------------------------------------------------------------------------- #
# 4. Alert email
# --------------------------------------------------------------------------- #


def _fmt_price(p: Any) -> str:
    try:
        return f"${float(p):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def _build_verification_alert(
    needs_posting: list[dict], needs_update: list[dict]
) -> str:
    out: list[str] = [
        f"MERCEDES-BENZ OF DURHAM — AD POSTING ALERT  {date.today().isoformat()}",
        "",
        f"SECTION 1 — ADS NOT POSTED ({len(needs_posting)} vehicles)",
        "=" * 60,
    ]
    if not needs_posting:
        out.append("(none)")
    for r in needs_posting:
        e = r["entry"]
        days = r.get("days_since_ad")
        days_txt = f"{days} days since ad written" if days is not None else "age unknown"
        out += [
            "",
            f"Stock: {r['stock_number']} | {r['year_make_model']} | {days_txt}",
            f"Ad written: {e.get('first_ad_date', 'n/a')} | "
            f"Current price: {_fmt_price(e.get('last_price_at_write'))}",
            f"Verdict: {r['compare']['verdict']}  "
            f"(VDP: {r['check'].get('url_found') or 'not found on site'})",
            "",
            e.get("current_ad_text", "(no stored ad text)"),
            "-" * 60,
        ]

    out += [
        "",
        f"SECTION 2 — ADS NEED UPDATE ({len(needs_update)} vehicles)",
        "=" * 60,
    ]
    if not needs_update:
        out.append("(none)")
    for r in needs_update:
        e = r["entry"]
        cmp = r["compare"]
        out += [
            "",
            f"Stock: {r['stock_number']} | {r['year_make_model']}",
            "Issue: Ad posted but outdated — reprice not reflected "
            f"(match score {cmp['match_score']}, missing: "
            f"{', '.join(cmp['missing_phrases']) or 'n/a'})",
            f"VDP: {r['check'].get('url_found') or 'n/a'}",
            "",
            e.get("current_ad_text", "(no stored ad text)"),
            "-" * 60,
        ]

    return "\n".join(out)


def send_verification_alert(
    needs_posting: list[dict], needs_update: list[dict], *, send: bool = True
) -> str | None:
    """Email the posting alert. Sends only if there is at least one vehicle in
    either bucket. Returns the email body (or None if nothing to send)."""
    if not needs_posting and not needs_update:
        print("[verify] nothing needs posting or updating — no alert sent.")
        return None

    body = _build_verification_alert(needs_posting, needs_update)
    subject = f"Mercedes-Benz of Durham — Ad Posting Alert {date.today().isoformat()}"
    if send:
        try:
            from adwriter import _send_gmail

            _send_gmail(subject, body)
        except Exception as exc:  # noqa: BLE001
            print(f"[verify] could not send alert email: {exc}", file=sys.stderr)
    return body


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check whether ads are posted and current on HendrickCars.com"
    )
    parser.add_argument("stock", nargs="?", help="single stock number to check")
    parser.add_argument(
        "--all", action="store_true", help="run verification across ad_history.json"
    )
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument(
        "--no-email", action="store_true", help="do not send the alert email"
    )
    args = parser.parse_args(argv)
    headless = not args.headed

    # The orchestrator holds this for its whole run and keeps ad_history in
    # memory across it; running alongside would clobber each other's writes.
    try:
        acquire_scraper_lock(ORCHESTRATOR_LOCK_PATH, wait_seconds=0)
    except ScraperBusyError:
        print("[verify] orchestrator is running — skipping to protect ad_history.json")
        return 0
    try:
        return _main_locked(args, parser, headless)
    finally:
        release_lock_if_owned(ORCHESTRATOR_LOCK_PATH)


def _main_locked(
    args: argparse.Namespace, parser: argparse.ArgumentParser, headless: bool
) -> int:
    if args.all:
        current, needs_posting, needs_update = run_verification(headless=headless)
        print(
            f"\nchecked {len(current) + len(needs_posting) + len(needs_update)} | "
            f"current {len(current)} | not posted {len(needs_posting)} | "
            f"outdated {len(needs_update)}"
        )
        send_verification_alert(needs_posting, needs_update, send=not args.no_email)
        return 0

    if not args.stock:
        parser.error("give a stock number, or --all")

    from adwriter import load_ad_history

    stock = args.stock.strip().lstrip("#").upper()
    live = check_hendrickcars(stock, headless=headless)
    print(f"page_found: {live['page_found']}  url: {live['url_found']}")
    print(f"description ({len(live['description_text'])} chars):")
    print(live["description_text"][:600] or "(empty)")

    entry = load_ad_history().get(stock)
    if entry and entry.get("current_ad_text"):
        cmp = compare_ad(
            entry["current_ad_text"],
            live["description_text"] if live["page_found"] else None,
        )
        print(f"\nverdict: {cmp['verdict']}  score: {cmp['match_score']}")
        print(f"matched:  {cmp['matched_phrases']}")
        print(f"missing:  {cmp['missing_phrases']}")
    else:
        print("\n(no stored ad in ad_history.json for this stock — comparison skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
