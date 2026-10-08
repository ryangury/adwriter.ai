"""billing.py — Anthropic Admin API cost / usage reports for the Spending page.

    cost_report              GET /v1/organizations/cost_report
        starting_at (RFC 3339), ending_at, bucket_width=1d, limit (max 31),
        page, group_by[]=description (puts the model on every line).
        `amount` is in the lowest currency unit (cents) as a decimal string.
    usage_report/messages    GET /v1/organizations/usage_report/messages
        same paging; group_by[]=model gives tokens per model.

Both are followed through next_page until has_more is false. Arrays go on the
wire as group_by[]=..., never group_by=.... Days are UTC buckets. The key is
only ever placed in a request header; no message here includes it.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests

ANTHROPIC_VERSION = "2023-06-01"
COST_URL = "https://api.anthropic.com/v1/organizations/cost_report"
USAGE_URL = "https://api.anthropic.com/v1/organizations/usage_report/messages"
PAGE_LIMIT = 31
MAX_PAGES = 40  # hard cap against a pathological pagination loop
CACHE_TTL_S = 3600
FAMILIES = ("sonnet", "haiku", "web search", "other")
_TOKEN_FIELDS = ("uncached_input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


class BillingError(RuntimeError):
    """A report request failed. status: HTTP status (None for a network error);
    api_message: the API's own error message, when it sent one."""

    def __init__(self, report: str, status: int | None, api_message: str | None, detail: str | None = None):
        self.report, self.status, self.api_message = report, status, api_message
        super().__init__(detail or f"HTTP {status} from {report}: {api_message or 'no message'}")


def _api_message(resp: requests.Response) -> str | None:
    try:
        err = resp.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else None
    except (ValueError, AttributeError):
        msg = None
    return (msg or (resp.text or "")[:200] or None)


def fetch_all(url: str, params: list[tuple[str, Any]], key: str, *, get: Callable = requests.get) -> list[dict]:
    """Every bucket of a report: GET with `params` (a list of pairs, so the
    group_by[] array form is kept), following next_page until has_more is false."""
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    report = url.rsplit("/", 1)[-1] if "usage_report" not in url else "usage_report/messages"
    buckets: list[dict] = []
    page: str | None = None
    for _ in range(MAX_PAGES):
        q = list(params) + ([("page", page)] if page else [])
        try:
            resp = get(url, params=q, headers=headers, timeout=30)
        except requests.RequestException as exc:
            raise BillingError(report, None, None, f"could not reach Anthropic for {report} ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise BillingError(report, resp.status_code, _api_message(resp))
        body = resp.json()
        buckets.extend(body.get("data") or [])
        if not body.get("has_more") or not body.get("next_page"):
            return buckets
        page = body["next_page"]
    raise BillingError(report, None, None, f"{report}: more than {MAX_PAGES} pages; stopped")


def family(model: str | None, cost_type: str | None = None) -> str:
    m = (model or "").lower()
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    if cost_type == "web_search":
        return "web search"
    return "other"


def parse_cost(buckets: list[dict]) -> dict[str, Any]:
    """{"total", "by_family", "days": [{day (UTC), sonnet, haiku, web search, other, total}]}
    in DOLLARS (the API's amounts are cents)."""
    days = []
    by_family = dict.fromkeys(FAMILIES, 0.0)
    for b in buckets:
        row = dict.fromkeys(FAMILIES, 0.0)
        for r in b.get("results") or []:
            try:
                usd = float(r.get("amount")) / 100.0
            except (TypeError, ValueError):
                continue
            row[family(r.get("model"), r.get("cost_type"))] += usd
        for f in FAMILIES:
            by_family[f] += row[f]
        days.append({"day": (b.get("starting_at") or "")[:10], **row, "total": sum(row.values())})
    return {"total": sum(by_family.values()), "by_family": by_family, "days": days}


def parse_tokens(buckets: list[dict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for b in buckets:
        for r in b.get("results") or []:
            model = r.get("model") or "unknown"
            totals[model] = totals.get(model, 0) + sum(
                v for k in _TOKEN_FIELDS if isinstance((v := r.get(k)), (int, float)))
    return totals


def describe_error(exc: Exception) -> str:
    """The text the page shows: the HTTP status and the API's own message; the
    key is blamed only for a 401 / 403."""
    if isinstance(exc, BillingError):
        if exc.status in (401, 403):
            return (f"Billing data unavailable: Anthropic rejected the Admin key for {exc.report} "
                    f"(HTTP {exc.status}: {exc.api_message or 'no message'}). Check "
                    f"ANTHROPIC_BILLING_COST_API_KEY in credentials.py.")
        if exc.status is None:
            return f"Billing data unavailable: {exc}."
        return f"Billing data unavailable: {exc.report} returned HTTP {exc.status}: {exc.api_message or 'no message'}."
    return f"Billing data unavailable: {type(exc).__name__}."


def fetch_month(key: str, now: datetime | None = None, *, get: Callable = requests.get) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    base = [("starting_at", start.strftime("%Y-%m-%dT%H:%M:%SZ")), ("ending_at", now.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("bucket_width", "1d"), ("limit", PAGE_LIMIT)]
    cost = parse_cost(fetch_all(COST_URL, base + [("group_by[]", "description")], key, get=get))
    tokens = parse_tokens(fetch_all(USAGE_URL, base + [("group_by[]", "model")], key, get=get))
    return {**cost, "tokens": tokens}


_cache: dict[str, Any] = {"fetched_at": 0.0, "data": None}


def get_billing(key: str | None, *, now_s: float | None = None, get: Callable = requests.get,
                cache: dict[str, Any] | None = None) -> dict[str, Any]:
    """{"data", "fetched_at", "error"}. Only successes are cached (one hour); a
    failure is never stored, so the next page load retries. When a refresh fails
    but an earlier success exists, that older data comes back with the error and
    its own fetched_at, so the page can show its age."""
    cache = _cache if cache is None else cache
    now_s = time.time() if now_s is None else now_s
    if cache["data"] is not None and now_s - cache["fetched_at"] < CACHE_TTL_S:
        return {"data": cache["data"], "fetched_at": cache["fetched_at"], "error": None}
    if not key:
        return {"data": cache["data"], "fetched_at": cache["fetched_at"] or None,
                "error": "Billing data unavailable: ANTHROPIC_BILLING_COST_API_KEY is not set in credentials.py."}
    try:
        data = fetch_month(key, get=get)
    except Exception as exc:  # noqa: BLE001 - the page must never crash
        return {"data": cache["data"], "fetched_at": cache["fetched_at"] or None, "error": describe_error(exc)}
    cache["data"], cache["fetched_at"] = data, now_s
    return {"data": data, "fetched_at": now_s, "error": None}


def age_text(fetched_at: float | None, now_s: float | None = None) -> str | None:
    if not fetched_at:
        return None
    s = max(0, int((time.time() if now_s is None else now_s) - fetched_at))
    return "just now" if s < 60 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h {(s % 3600) // 60} min ago"
