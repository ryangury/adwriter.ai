"""api_cost.py - one place that logs every model call, prices it, and picks the key.

Every call to the Anthropic API in this repo goes through here:

    create(client, purpose=..., stock=..., **kwargs)   a messages.create call
    record(response, purpose=..., stock=..., ...)      a response the caller got
                                                       itself (bounded_search streams)
    make_client(max_retries=None)                      the client, main or dev key

and writes one row to the api_cost_log table (api_cost.db, SQLite): timestamp
(UTC, like the Console's day buckets), purpose, stock, model, input / output /
cache-write / cache-read tokens, web_search_requests, cost_usd, stop_reason,
retry flag, which class of key was used, and a label. Logging never raises: a
failed write prints one line and the call goes on.

Not logged: messages.count_tokens (adwriter._estimate_tokens) - it is a free
endpoint with no usage and no cost.

Prices live in PRICES below and nowhere else. Cache writes are priced at 1.25x
the input rate (5-minute cache), cache reads at 0.1x, web search per request.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
# The one prices table (USD per million tokens: input, output)
# --------------------------------------------------------------------------- #
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
CACHE_WRITE_MULT = 1.25   # 5-minute cache write, times the input rate
CACHE_READ_MULT = 0.10
WEB_SEARCH_USD = 0.01     # $10 per 1,000 searches

PURPOSES = ("generate", "reprice", "recon_update", "tow_lookup", "range_lookup",
            "research_search", "vision", "sticker_warmup", "vision_processor", "test", "other")

# --------------------------------------------------------------------------- #
# Spend limits
#
# 1. The ad-writing call (generate_ad with web search) - the DEGRADE guard, OFF
#    until enabled. Per call $0.75, per ad $1.50 counting every retry. At 6 web
#    searches, or when either budget is near (GUARD_NEAR_FRACTION of it), the
#    search tool is dropped from what is left of the ad: the model writes from
#    the data package alone. It never fails the ad and never saves a partial;
#    every degrade is logged (a zero-cost api_cost_log row, stop_reason
#    "degraded: ...", and a stderr line).
# 2. Tow and range lookups - an ABORT cap by actual logged cost per
#    configuration (LOOKUP_COST_CAP_USD), always on. Nothing is cached on an abort.
# --------------------------------------------------------------------------- #
GENERATE_GUARD_ENABLED = False
GENERATE_CALL_BUDGET_USD = 0.75
GENERATE_AD_BUDGET_USD = 1.50
GENERATE_MAX_SEARCHES = 6
GUARD_NEAR_FRACTION = 0.80
LOOKUP_COST_CAP_USD = 0.40

AD_SPENT: dict[str, float] = {}
AD_PURPOSES = ("generate", "research_search")


def begin_ad(stock: str | None) -> None:
    """A new build of this stock starts: its per-ad spend restarts at zero."""
    if stock:
        AD_SPENT[stock] = 0.0


def ad_spent(stock: str | None) -> float:
    return AD_SPENT.get(stock or "", 0.0)


class SearchDegrade(Exception):
    """Not an error: the search conversation stops here and the caller writes
    the ad from the data package without the search tool."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


class Guard:
    """Running totals of one generate call's search conversation."""

    def __init__(self, stock: str | None = None, call_budget: float = GENERATE_CALL_BUDGET_USD,
                 ad_budget: float = GENERATE_AD_BUDGET_USD, max_searches: int = GENERATE_MAX_SEARCHES,
                 near: float = GUARD_NEAR_FRACTION):
        self.call_budget, self.ad_budget, self.max_searches, self.near = call_budget, ad_budget, max_searches, near
        self.ad_before = ad_spent(stock)
        self.cost = 0.0
        self.searches = 0

    def add(self, cost: float | None, searches: int = 0) -> None:
        self.cost += cost or 0.0
        self.searches += searches

    def tool_max_uses(self, base: int | None = None) -> int:
        """max_uses for the next request: never more than the searches left."""
        left = max(1, self.max_searches - self.searches)
        return left if base is None else min(base, left)

    def degrade_reason(self) -> str | None:
        if self.searches >= self.max_searches:
            return "searches"
        if self.cost >= self.near * self.call_budget:
            return "call budget"
        if self.ad_before + self.cost >= self.near * self.ad_budget:
            return "ad budget"
        return None


def generate_guard(stock: str | None = None) -> Guard | None:
    return Guard(stock) if GENERATE_GUARD_ENABLED else None


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS api_cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                -- UTC, ISO 8601
    purpose TEXT NOT NULL,
    stock TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,                   -- NULL: the model is not in PRICES
    stop_reason TEXT,
    retry INTEGER NOT NULL DEFAULT 0,
    key_class TEXT,                  -- production | dev
    workspace_id TEXT,               -- the anthropic-workspace-id response header, when the call returned one
    label TEXT
);
CREATE INDEX IF NOT EXISTS ix_api_cost_log_ts ON api_cost_log (ts);
CREATE INDEX IF NOT EXISTS ix_api_cost_log_stock ON api_cost_log (stock, ts);
"""


def db_path() -> Path:
    return Path(os.environ.get("ADWRITER_COST_DB") or ROOT / "api_cost.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(api_cost_log)")}
    if "workspace_id" not in cols:                 # a log written before the column existed
        conn.execute("ALTER TABLE api_cost_log ADD COLUMN workspace_id TEXT")
    return conn


def price_of(model: str | None) -> tuple[float, float] | None:
    """(input, output) $/MTok for a model id; a dated id matches its undated key."""
    m = (model or "").lower()
    best = max((k for k in PRICES if m.startswith(k)), key=len, default=None)
    return PRICES[best] if best else None


def cost_usd(model: str | None, input_tokens: int = 0, output_tokens: int = 0,
             cache_creation: int = 0, cache_read: int = 0, searches: int = 0) -> float | None:
    p = price_of(model)
    if p is None:
        return None
    inp, out = p
    return (input_tokens * inp + output_tokens * out
            + cache_creation * inp * CACHE_WRITE_MULT + cache_read * inp * CACHE_READ_MULT) / 1e6 \
        + searches * WEB_SEARCH_USD


def _int(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def usage_of(response: Any) -> dict[str, int]:
    u = getattr(response, "usage", None)
    stu = getattr(u, "server_tool_use", None)
    return {
        "input_tokens": _int(getattr(u, "input_tokens", 0)),
        "output_tokens": _int(getattr(u, "output_tokens", 0)),
        "cache_creation_tokens": _int(getattr(u, "cache_creation_input_tokens", 0)),
        "cache_read_tokens": _int(getattr(u, "cache_read_input_tokens", 0)),
        "web_search_requests": _int(getattr(stu, "web_search_requests", 0)),
    }


def record(response: Any = None, *, purpose: str, stock: str | None = None, model: str | None = None,
           retry: bool = False, label: str | None = None, stop_reason: str | None = None,
           searches: int | None = None, workspace_id: str | None = None) -> float | None:
    """Write one api_cost_log row; returns its cost_usd. `response` None logs a
    call that produced no response (an aborted or failed request: tokens 0, the
    reason in stop_reason). Never raises."""
    try:
        purpose = os.environ.get("ADWRITER_COST_PURPOSE") or _process_purpose or purpose
        if purpose not in PURPOSES:
            purpose = "other"
        u = usage_of(response)
        if searches is not None:
            u["web_search_requests"] = searches
        model = model or getattr(response, "model", None)
        cost = cost_usd(model, u["input_tokens"], u["output_tokens"], u["cache_creation_tokens"],
                        u["cache_read_tokens"], u["web_search_requests"])
        stop = stop_reason or getattr(response, "stop_reason", None)
        if stock and purpose in AD_PURPOSES and cost:
            AD_SPENT[stock] = AD_SPENT.get(stock, 0.0) + cost
        with contextlib.closing(_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO api_cost_log (ts, purpose, stock, model, input_tokens, output_tokens, "
                "cache_creation_tokens, cache_read_tokens, web_search_requests, cost_usd, stop_reason, "
                "retry, key_class, workspace_id, label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), purpose, stock, model,
                 u["input_tokens"], u["output_tokens"], u["cache_creation_tokens"], u["cache_read_tokens"],
                 u["web_search_requests"], cost, stop, 1 if retry else 0, current_key_class(), workspace_id,
                 (label or "")[:200] or None),
            )
        return cost
    except Exception as exc:  # noqa: BLE001 - cost logging must never sink a call
        print(f"[api_cost] could not log a {purpose} call: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


WORKSPACE_HEADER = "anthropic-workspace-id"


def _is_real_client(client: Any) -> bool:
    """True for an anthropic SDK client (not a test stub): only then is the raw
    response, with its headers, asked for."""
    return type(client).__module__.split(".")[0] == "anthropic" and hasattr(
        getattr(client, "messages", None), "with_raw_response")


def workspace_of(obj: Any) -> str | None:
    """The anthropic-workspace-id header of a raw response / stream, or None."""
    for path in (("headers",), ("response", "headers")):
        cur = obj
        for a in path:
            cur = getattr(cur, a, None)
        try:
            v = cur.get(WORKSPACE_HEADER) if cur is not None else None
        except Exception:  # noqa: BLE001
            v = None
        if isinstance(v, str) and v:
            return v
    return None


def create(client: Any, *, purpose: str, stock: str | None = None, retry: bool = False,
           label: str | None = None, **kwargs: Any) -> Any:
    """client.messages.create(**kwargs), logged. An exception is logged as a
    zero-token row (stop_reason 'error: <class>') and re-raised."""
    workspace = None
    try:
        if _is_real_client(client):
            raw = client.messages.with_raw_response.create(**kwargs)
            workspace = workspace_of(raw)
            response = raw.parse()
        else:
            response = client.messages.create(**kwargs)
    except Exception as exc:
        record(None, purpose=purpose, stock=stock, model=kwargs.get("model"), retry=retry, label=label,
               stop_reason=f"error: {type(exc).__name__}")
        raise
    record(response, purpose=purpose, stock=stock, model=kwargs.get("model"), retry=retry, label=label,
           workspace_id=workspace)
    return response


# --------------------------------------------------------------------------- #
# Which key: production or dev
#
# A process uses the DEV key unless it declared itself production: the
# orchestrator, the website, sticker_warmup and vision_processor call
# use_production_key() at start. Test harnesses (run_tests sets
# ADWRITER_KEY_CLASS=dev), tow_refresh --lookup, the refresh and draft scripts
# and any ad-hoc script that imports this repo therefore get the dev key. The
# environment variable ADWRITER_KEY_CLASS (dev | production) beats the process's
# own declaration. A missing dev key is an error, never a fallback to production.
# --------------------------------------------------------------------------- #
PRODUCTION, DEV = "production", "dev"
_declared: str | None = None
_process_purpose: str | None = None       # a job's own cost-log label (sticker_warmup, vision_processor)
_announced: set[str] = set()


class DevKeyMissing(RuntimeError):
    """The dev key was wanted and credentials.py has no ANTHROPIC_API_KEY_DEV."""


def _announce(key_class: str) -> None:
    msg = f"[api] key class: {key_class.upper()}" + (" (ANTHROPIC_API_KEY)" if key_class == PRODUCTION else " (ANTHROPIC_API_KEY_DEV)")
    if msg not in _announced:
        _announced.add(msg)
        print(msg, file=sys.stderr, flush=True)


def use_production_key(purpose: str | None = None) -> None:
    """Called at start by the production entry points. Prints the class. With
    `purpose`, every call this process logs carries that label."""
    global _declared, _process_purpose
    _declared = PRODUCTION
    _process_purpose = purpose
    _announce(current_key_class())


def use_dev_key() -> None:
    """Called at start by ad-hoc entry points (dev is also the default). Prints the class."""
    global _declared
    _declared = DEV
    _announce(current_key_class())


def current_key_class() -> str:
    env = (os.environ.get("ADWRITER_KEY_CLASS") or "").strip().lower()
    if env in ("dev", PRODUCTION, "main"):
        return PRODUCTION if env in (PRODUCTION, "main") else DEV
    return _declared or DEV


def _credentials() -> Any:
    import credentials
    return credentials


def api_key() -> str:
    c = _credentials()
    key_class = current_key_class()
    if key_class == DEV:
        key = getattr(c, "ANTHROPIC_API_KEY_DEV", None)
        if not key:
            raise DevKeyMissing(
                "the dev key is required here (test harness / ad-hoc script / lookup) but credentials.py has no "
                "ANTHROPIC_API_KEY_DEV; not falling back to the production key")
    else:
        key = c.ANTHROPIC_API_KEY
    _announce(key_class)
    return key


def make_client(max_retries: int | None = None) -> Any:
    """anthropic.Anthropic with the right key. The key itself is never printed."""
    import anthropic

    kw: dict[str, Any] = {"api_key": api_key()}
    if max_retries is not None:
        kw["max_retries"] = max_retries
    return anthropic.Anthropic(**kw)


# --------------------------------------------------------------------------- #
# Reads for the Spending page
# --------------------------------------------------------------------------- #
def month_bounds(now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")


def rows_since(start_iso: str, path: Path | None = None) -> list[sqlite3.Row]:
    p = path or db_path()
    if not Path(p).exists():
        return []
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        return conn.execute("SELECT * FROM api_cost_log WHERE ts >= ? ORDER BY ts", (start_iso,)).fetchall()
    finally:
        conn.close()
