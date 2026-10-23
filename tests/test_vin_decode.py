"""VIN decode (vin_decode.py) with recorded NHTSA vPIC responses (tests/fixtures/vpic): the decode fills a
configuration key's missing engine, labelled "VIN decode"; a decode that contradicts the sticker is used for neither
and flagged; a blank or failed decode falls back to today's behaviour and is negative-cached; a cache hit costs no
call; the classifier gets the decode as a flagged signal; nothing reaches ad copy. No network."""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path)

sys.path.insert(0, str(_paths.ROOT))
import feature_cache  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="vin_decode_test_"))
feature_cache.DB_PATH = TMP / "feature_cache.db"
import powertrain as P  # noqa: E402
import research as R  # noqa: E402
import towing as T  # noqa: E402
import vin_decode as V  # noqa: E402

T.DB_PATH = feature_cache.DB_PATH
FIX = Path(_paths.ROOT) / "tests" / "fixtures" / "vpic"
RX = "2T2BGMCA7KC033405"          # T23316A, 2019 Lexus RX 450h (sticker prints no engine)
SPRINTER = "W1W4DBHY4NT097953"    # CT23257A, 2022 Sprinter 2500 (diesel; no sticker engine line)
RX_STICKER = "TOWING PREP PACKAGE  8YR/100,000 mile hybrid battery/hybrid component"


def recorded(vin):
    return (FIX / f"{vin}.json").read_text(encoding="utf-8")


class Calls:
    def __init__(self, fn=recorded):
        self.n, self.fn = 0, fn

    def __call__(self, vin):
        self.n += 1
        return self.fn(vin)


def rx_tow(cls="hybrid", mode="fetch"):
    return T.towing_for("2019 Lexus RX", "AWD", "SUV", RX_STICKER, [], vin=RX, powertrain_class=cls,
                        allow_lookup=False, vin_decode=mode)


class Base(unittest.TestCase):
    def setUp(self):
        V.clear()
        self.ov = mock.patch.object(T, "TOW_OVERRIDES_PATH", TMP / "tow_overrides.json")
        self.ov.start()
        self.addCleanup(self.ov.stop)
        (TMP / "tow_overrides.json").write_text("{}", encoding="utf-8")


class Decode(Base):
    def test_recorded_fields(self):
        d = V._results(recorded(RX))
        self.assertEqual((d["Trim"], d["DisplacementL"], d["EngineCylinders"]), ("450h Standard", "3.5", "6"))
        self.assertEqual(V.engine(d)["text"], "3.5L 6-cylinder, hybrid")
        self.assertEqual(V.engine(d)["phrase"], "3.5L 6-cylinder hybrid")
        self.assertEqual(V.decode_class(V._results(recorded(SPRINTER))), "diesel")
        self.assertIsNone(V.decode_class(V._results(recorded("4JGFB4FB0SB414497"))), "blank electrification: no signal")

    def test_only_the_allowed_fields_are_read(self):
        d = V._results(recorded(RX))
        base = V.engine(d)
        changed = dict(d, Trim="Other", DriveType="FWD", BodyClass="Pickup", CabType="Crew", BedLengthIN="78",
                       EngineConfiguration="In-Line", EngineModel="X")
        self.assertEqual(V.engine(changed), base, "trim, drive type, body, cab, bed and layout never change the result")
        self.assertEqual(set(V.USED_FIELDS), {"DisplacementL", "EngineCylinders", "FuelTypePrimary", "ElectrificationLevel", "Series"})

    def test_decode_fills_the_key(self):
        calls = Calls()
        with mock.patch.object(V, "_http", calls):
            before = rx_tow(mode=None)
            self.assertEqual(before["config"]["missing"], ["engine"], "today's behaviour without a decode")
            after = rx_tow()
        cfg = after["config"]
        self.assertEqual((cfg["engine"], cfg["engine_source"], cfg["missing"]), ("3.5L 6-cylinder hybrid", "VIN decode", []))
        self.assertEqual(calls.n, 1)
        self.assertIn("3.5L 6-cylinder hybrid", after["config_text"])
        self.assertIsNotNone(R.key_of("tow", cfg), "a normal row now: entries and Find candidates")
        self.assertIsNone(P.engine_sentence(RX_STICKER), "never in ad copy: the engine sentence is the sticker's alone")

    def test_the_sticker_disagrees(self):
        with mock.patch.object(V, "_http", Calls()):
            t = rx_tow(cls="gas")
        cfg = t["config"]
        self.assertEqual((cfg["engine"], cfg["missing"]), (None, ["engine"]), "neither is used")
        self.assertIn("VIN decode says 3.5L 6-cylinder, hybrid (hybrid); the sticker reads gas", cfg["vin_decode_conflict"])

    def test_classifier_signal_is_flagged_on_disagreement(self):
        with mock.patch.object(V, "_http", Calls()):
            V.get(SPRINTER, fetch=True)
        with mock.patch.object(P, "cached_texts", return_value=("", "")), \
             mock.patch.object(P, "classify", return_value={"class": "gas", "label": "gas", "flag": None}):
            pt = P.vehicle_powertrain(SPRINTER, "2022 Mercedes-Benz Sprinter", "RWD", body_style="Van")
        self.assertEqual(pt["class"], "gas", "the class is kept")
        self.assertTrue(pt["vin_decode_disagrees"])
        self.assertIn("VIN decode says diesel, the classifier says gas", " ".join(pt["flags"]))

    def test_blank_or_failed_decode_falls_back_and_is_negative_cached(self):
        def boom(vin):
            raise TimeoutError("timed out")
        calls = Calls(boom)
        with mock.patch.object(V, "_http", calls):
            t = rx_tow()
            self.assertEqual(t["config"]["missing"], ["engine"], "today's behaviour")
            rx_tow()
            self.assertEqual(calls.n, 1, "a failure is cached: no second call")
            self.assertEqual(V.cached(RX)["ok"], 0)
        blank = json.loads(recorded(RX))
        blank["Results"][0]["DisplacementL"] = ""
        self.assertIsNone(V.get("1FM5K8GCXPGB03955", fetch=True, http=lambda v: json.dumps(blank)))
        self.assertIn("blank decode", V.cached("1FM5K8GCXPGB03955")["error"])
        later = Calls()
        V.get(RX, fetch=True, http=later, today=date.today() + timedelta(days=V.NEGATIVE_RETRY_DAYS))
        self.assertEqual(later.n, 1, "retried after the negative-cache period")

    def test_a_cache_hit_costs_no_call(self):
        V.get(RX, fetch=True, http=recorded)
        with mock.patch.object(V, "_http", side_effect=AssertionError("no call on a cache hit")):
            self.assertEqual(V.engine(V.get(RX, fetch=True))["text"], "3.5L 6-cylinder, hybrid")
            self.assertEqual(rx_tow()["config"]["engine_source"], "VIN decode")

    def test_cache_only_callers_never_fetch(self):
        with mock.patch.object(V, "_http", side_effect=AssertionError("cache only")):
            self.assertEqual(rx_tow(mode="cache")["config"]["missing"], ["engine"])
            self.assertIsNone(V.get(RX))

    def test_research_row_shows_the_engine_note(self):
        V.get(RX, fetch=True, http=recorded)
        cfg = rx_tow(mode="cache")["config"]
        item = R.items_for(None, {"triggered": True, "rating": None, "note": "not looked up yet", "config": cfg,
                                  "config_text": T.describe(cfg)})
        item[0]["config"] = cfg
        with mock.patch.object(R, "live_items", return_value=[{"stock": "T23316A", "ymm": "2019 Lexus RX", "items": item}]), \
             mock.patch("tow_refresh._old_cache_rows", return_value=[]):
            (g,) = R.page_rows([{"stock_number": "T23316A", "vin": RX}], {})["sections"]["tow_missing"]
        self.assertFalse(g["needs_vin_override"])
        self.assertEqual(g["engine_note"], "Engine from VIN decode: 3.5L 6-cylinder, hybrid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
