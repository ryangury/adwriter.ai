#!/usr/bin/env python3
"""vin_decode.py — NHTSA vPIC VIN decode, cached by VIN, used only to complete a configuration key.

DecodeVinValues (no key, JSON): one request per second at most, a 10-second timeout. A decode is cached in the
vin_decode table of feature_cache.db with its raw response and fetch date and is never refreshed (clear() removes
it); a failed or blank decode is cached too (negative cache) and retried after NEGATIVE_RETRY_DAYS. Every failure
falls back to today's behaviour: the caller gets None.

Where it may fetch: the Research page load and the nightly research job (rows whose configuration is incomplete),
and build-time key completion (towing_for with vin_decode="fetch"). Everywhere else reads the cache only. Its
engine (displacement, cylinders, fuel, electrification) completes the configuration key's engine only when the
sticker and ACV Max supply none, labelled "VIN decode"; it is never used in ad copy (engine sentences stay limited
to the sticker's printed words). Its electrification level is one more powertrain signal, flagged on disagreement.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import date
from typing import Any

import feature_cache

URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"
TIMEOUT_S = 10
MIN_INTERVAL_S = 1.0
NEGATIVE_RETRY_DAYS = 7
SOURCE_LABEL = "VIN decode"
_last_call = [0.0]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vin_decode (
    vin           TEXT PRIMARY KEY,
    raw           TEXT,                 -- the response body (a failure: NULL)
    fetched_date  TEXT NOT NULL,
    ok            INTEGER NOT NULL,     -- 1: decoded with an engine; 0: failed or blank (negative cache)
    error         TEXT
);
"""


@contextlib.contextmanager
def _connect():
    conn = sqlite3.connect(str(feature_cache.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


def _http(vin: str) -> str:
    """One vPIC request (throttled to one per second)."""
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()
    req = urllib.request.Request(URL.format(vin=vin), headers={"User-Agent": "adwriter-research/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read().decode("utf-8")


def _results(raw: str | None) -> dict[str, Any] | None:
    try:
        res = (json.loads(raw or "") or {}).get("Results") or []
    except ValueError:
        return None
    return res[0] if res and isinstance(res[0], dict) else None


def cached(vin: str) -> dict[str, Any] | None:
    """The cached row for this VIN, or None."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM vin_decode WHERE vin=?", ((vin or "").strip().upper(),)).fetchone()
    return dict(row) if row else None


def clear(vin: str | None = None) -> int:
    """Remove one VIN's decode (or every decode) so the next allowed call fetches again."""
    with _connect() as conn:
        if vin:
            return conn.execute("DELETE FROM vin_decode WHERE vin=?", (vin.strip().upper(),)).rowcount
        return conn.execute("DELETE FROM vin_decode").rowcount


def get(vin: str | None, *, fetch: bool = False, http: Any = None, today: date | None = None) -> dict[str, Any] | None:
    """The decoded fields (vPIC Results[0]) for this VIN, or None. A cached decode never costs a call. With fetch
    False only the cache is read. Never raises."""
    vin = (vin or "").strip().upper()
    if len(vin) != 17:
        return None
    today = today or date.today()
    try:
        row = cached(vin)
        if row and row["ok"]:
            return _results(row["raw"])
        if row and not row["ok"]:
            try:
                age = (today - date.fromisoformat(row["fetched_date"])).days
            except ValueError:
                age = NEGATIVE_RETRY_DAYS
            if age < NEGATIVE_RETRY_DAYS or not fetch:
                return None
        if not fetch:
            return None
        try:
            raw = (http or _http)(vin)
            d = _results(raw)
            err = None if d and engine(d) else ("blank decode (no displacement)" if d else "unreadable response")
        except Exception as exc:  # noqa: BLE001 - a decode never blocks anything
            raw, d, err = None, None, f"{type(exc).__name__}: {exc}"
        with _connect() as conn:
            conn.execute("INSERT INTO vin_decode (vin, raw, fetched_date, ok, error) VALUES (?,?,?,?,?) "
                         "ON CONFLICT (vin) DO UPDATE SET raw=excluded.raw, fetched_date=excluded.fetched_date, "
                         "ok=excluded.ok, error=excluded.error", (vin, raw, today.isoformat(), 0 if err else 1, err))
        if err:
            print(f"[vin-decode] {vin}: {err} (cached for {NEGATIVE_RETRY_DAYS} days)", file=sys.stderr)
            return None
        return d
    except Exception as exc:  # noqa: BLE001
        print(f"[vin-decode] {vin}: cache unavailable ({exc})", file=sys.stderr)
        return None


_ELECTRIFICATION = {"strong hev": "hybrid", "hev": "hybrid", "mild hev": "mild_hybrid", "phev": "phev", "bev": "bev"}


def electrification(d: dict[str, Any] | None) -> str | None:
    """hybrid / mild_hybrid / phev / bev from ElectrificationLevel ("HEV - Level Unknown" -> "hybrid_any"), else None."""
    lvl = ((d or {}).get("ElectrificationLevel") or "").strip().lower()
    if not lvl:
        return None
    if "level unknown" in lvl:
        return "hybrid_any"
    for k, v in sorted(_ELECTRIFICATION.items(), key=lambda kv: -len(kv[0])):
        if lvl.startswith(k):
            return v
    return None


def decode_class(d: dict[str, Any] | None) -> str | None:
    """A powertrain-class signal from the decode, or None (a gasoline car with no electrification gives no signal:
    vPIC leaves mild hybrids blank)."""
    e = electrification(d)
    if e:
        return e
    if ((d or {}).get("FuelTypePrimary") or "").strip().lower() == "diesel":
        return "diesel"
    return None


def class_disagrees(signal: str | None, cls: str | None) -> bool:
    """True when the decode's signal contradicts the classifier's class (unknown / no signal never disagrees)."""
    if not signal or not cls or cls == "unknown":
        return False
    if signal == "hybrid_any":
        return cls not in ("hybrid", "mild_hybrid", "phev")
    return signal != cls


def engine(d: dict[str, Any] | None) -> dict[str, Any] | None:
    """{"displacement", "cylinders", "layout", "fuel", "electrification", "phrase" (key form, e.g. "3.5L V6 hybrid"),
    "text" (display, e.g. "3.5L V6, hybrid")} or None when the decode has no displacement."""
    d = d or {}
    try:
        disp = float(d.get("DisplacementL") or "")
    except ValueError:
        return None
    if disp <= 0:
        return None
    cyl = (d.get("EngineCylinders") or "").strip()
    conf = (d.get("EngineConfiguration") or "").lower()
    layout = ("V" if conf.startswith("v") else "I-" if conf.startswith("in-line") or conf.startswith("inline") else
              "H" if conf.startswith("horizontal") else "")
    block = f"{layout}{cyl}" if cyl and layout else (f"{cyl}-cylinder" if cyl else "")
    fuel = (d.get("FuelTypePrimary") or "").strip().lower()
    elec = electrification(d)
    kind = {"hybrid": "hybrid", "hybrid_any": "hybrid", "mild_hybrid": "mild hybrid", "phev": "plug-in hybrid"}.get(elec or "")
    if not kind and fuel == "diesel":
        kind = "diesel"
    size = f"{disp:.1f}L"
    phrase = " ".join(p for p in (size, block, kind) if p)
    text = ", ".join(p for p in (" ".join(p for p in (size, block) if p), kind) if p)
    return {"displacement": size, "cylinders": int(cyl) if cyl.isdigit() else None, "layout": layout or None,
            "fuel": fuel or None, "electrification": elec, "phrase": phrase, "text": text}
