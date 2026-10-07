"""towing.lookup() limits: 120 s request timeout with one retry, an 8-minute
budget per configuration (skip, log, cache nothing), and towing_for(refresh=)
ignoring a cached result. The Anthropic client is stubbed; offline."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import anthropic  # noqa: E402
import towing as T  # noqa: E402

CFG = {"year": 2024, "make": "Mercedes-Benz", "model": "GLE", "engine": "GLE 450", "drivetrain": "AWD",
       "cab": "", "bed": "", "tow_package": "TRAILER HITCH", "missing": [], "truck": False, "ev": False}


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _client(create):
    return SimpleNamespace(messages=SimpleNamespace(create=create))


class Limits(unittest.TestCase):
    def test_client_and_per_request_timeout(self):
        seen = {}

        def make(**kw):
            seen["client"] = kw
            return _client(lambda **k: (seen.setdefault("req", k), SimpleNamespace(stop_reason="end_turn", content=[]))[1])

        with mock.patch.object(anthropic, "Anthropic", side_effect=make), \
             mock.patch.object(T, "allowed_domains", return_value=["mbusa.com"]):
            T.lookup(CFG)
        self.assertEqual(seen["client"]["timeout"], 120)
        self.assertEqual(seen["client"]["max_retries"], 1)
        self.assertLessEqual(seen["req"]["timeout"], 120)

    def test_budget_skips_after_8_minutes(self):
        clock = FakeClock()

        def create(**kw):
            clock.t += 5 * 60  # each pause_turn round takes 5 minutes
            return SimpleNamespace(stop_reason="pause_turn", content=[])

        with mock.patch.object(anthropic, "Anthropic", return_value=_client(create)), \
             mock.patch.object(T, "allowed_domains", return_value=["mbusa.com"]), \
             mock.patch.object(T, "_clock", clock):
            with self.assertRaises(T.TowLookupUnavailable) as cm:
                T.lookup(CFG)
        self.assertIn("8-minute budget", str(cm.exception))

    def test_timeout_is_unavailable_and_nothing_cached(self):
        def create(**kw):
            raise anthropic.APITimeoutError(request=mock.Mock())

        saved = []
        with mock.patch.object(anthropic, "Anthropic", return_value=_client(create)), \
             mock.patch.object(T, "allowed_domains", return_value=["mbusa.com"]), \
             mock.patch.object(T, "vehicle_config", return_value=CFG), \
             mock.patch.object(T, "get_cached", return_value=None), \
             mock.patch.object(T, "save", side_effect=lambda *a: saved.append(a)), \
             mock.patch.object(T, "load_tow_overrides", return_value={}):
            out = T.towing_for("2024 Mercedes-Benz GLE", "GLE 450", "SUV", "TRAILER HITCH", force=True)
        self.assertIsNone(out["rating"])
        self.assertIn("tow lookup failed", out["note"])
        self.assertEqual(saved, [])

    def test_refresh_ignores_the_cache(self):
        calls = []
        with mock.patch.object(T, "vehicle_config", return_value=CFG), \
             mock.patch.object(T, "get_cached", return_value={"tow_rating_lbs": None, "note": "rejected"}), \
             mock.patch.object(T, "lookup", side_effect=lambda c: calls.append(c) or {"lbs": 7700, "url": "u"}), \
             mock.patch.object(T, "save"), mock.patch.object(T, "load_tow_overrides", return_value={}):
            self.assertIsNone(T.towing_for("x", "y", "SUV", "TRAILER HITCH", force=True)["rating"])
            self.assertEqual(T.towing_for("x", "y", "SUV", "TRAILER HITCH", force=True, refresh=True)["rating"], 7700)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
