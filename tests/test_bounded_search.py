"""bounded_search.run_search and its users: streamed requests with a 120 s
silence abort, a time budget between events, at most 4 billed requests (retries
counted), one retry after silence; the tow lookup and electric-range lookup
turn a stop into 'unavailable' with nothing cached; the ad-writing call falls
back to writing without search; the build path never makes a live tow lookup.
The Anthropic client is a stub; offline."""
import _paths  # noqa: F401  (repo root first on sys.path; temp cost log)
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import anthropic  # noqa: E402
import bounded_search as B  # noqa: E402
import towing as T  # noqa: E402

CFG = {"year": 2024, "make": "Mercedes-Benz", "model": "GLE", "engine": "GLE 450", "drivetrain": "AWD",
       "cab": "", "bed": "", "tow_package": "TRAILER HITCH", "missing": [], "truck": False, "ev": False}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeStream:
    def __init__(self, script, clock):
        self.script, self.clock = script, clock

    def __enter__(self):
        if self.script.get("raise_on_enter"):
            raise self.script["raise_on_enter"]
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for dt in self.script.get("events", [0]):
            self.clock.t += dt
            yield object()

    def get_final_message(self):
        return SimpleNamespace(stop_reason=self.script.get("stop", "end_turn"), content=["c"])


class FakeClient:
    """messages.stream(...) pops one script per call; records every call."""

    def __init__(self, scripts, clock):
        self.scripts, self.clock, self.calls = list(scripts), clock, []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kw):
        self.calls.append(kw)
        return FakeStream(self.scripts.pop(0), self.clock)

    def with_options(self, **kw):
        self.options = kw
        return self


TIMEOUT = anthropic.APITimeoutError(request=mock.Mock())
KW = {"model": "m", "max_tokens": 10, "tools": [{"name": "web_search"}]}


def run(client, clock, **kw):
    logs = []
    kw.setdefault("log", logs.append)
    resp = B.run_search(client, kwargs=dict(KW), messages=[{"role": "user", "content": "q"}], label="t", clock=clock, **kw)
    return resp, logs


class RunSearch(unittest.TestCase):
    def test_streamed_with_a_120_second_silence_timeout_and_no_sdk_retries(self):
        clock = Clock()
        c = FakeClient([{"events": [1, 1]}], clock)
        resp, _ = run(c, clock)
        self.assertEqual(resp.stop_reason, "end_turn")
        self.assertEqual(c.calls[0]["timeout"], 120.0)
        self.assertEqual(c.options, {"max_retries": 0})
        self.assertEqual(c.calls[0]["model"], "m")

    def test_long_but_streaming_request_is_not_cut_off_by_the_silence_timeout(self):
        clock = Clock()
        c = FakeClient([{"events": [100] * 2}], clock)  # 200 s in total, never silent
        resp, logs = run(c, clock, budget_s=480)
        self.assertEqual(resp.stop_reason, "end_turn")
        self.assertTrue(any("SLOW" in m for m in logs), "a slow search is logged")

    def test_silence_then_one_retry_then_success(self):
        clock = Clock()
        c = FakeClient([{"raise_on_enter": TIMEOUT}, {"events": [1]}], clock)
        resp, logs = run(c, clock)
        self.assertEqual(resp.stop_reason, "end_turn")
        self.assertEqual(len(c.calls), 2)
        self.assertTrue(any("no data for 120 s" in m for m in logs))

    def test_silence_twice_stops(self):
        clock = Clock()
        c = FakeClient([{"raise_on_enter": TIMEOUT}, {"raise_on_enter": TIMEOUT}], clock)
        with self.assertRaises(B.SearchUnavailable) as cm:
            run(c, clock)
        self.assertEqual(cm.exception.reason, "silent")
        self.assertEqual(len(c.calls), 2)

    def test_budget_checked_between_events(self):
        clock = Clock()
        c = FakeClient([{"events": [200, 200, 200]}], clock)  # events keep arriving, past 480 s
        with self.assertRaises(B.SearchUnavailable) as cm:
            run(c, clock, budget_s=480)
        self.assertEqual(cm.exception.reason, "budget")

    def test_at_most_four_billed_requests_with_pause_turn(self):
        clock = Clock()
        c = FakeClient([{"events": [1], "stop": "pause_turn"}] * 6, clock)
        with self.assertRaises(B.SearchUnavailable) as cm:
            run(c, clock)
        self.assertEqual(cm.exception.reason, "limit")
        self.assertEqual(len(c.calls), 4)

    def test_retry_counts_toward_the_four(self):
        clock = Clock()
        c = FakeClient([{"events": [1], "stop": "pause_turn"}, {"events": [1], "stop": "pause_turn"},
                        {"raise_on_enter": TIMEOUT}, {"events": [1], "stop": "pause_turn"}, {"events": [1]}], clock)
        with self.assertRaises(B.SearchUnavailable) as cm:
            run(c, clock)
        self.assertEqual(cm.exception.reason, "limit")
        self.assertEqual(len(c.calls), 4)

    def test_api_error_is_unavailable(self):
        clock = Clock()
        err = anthropic.APIConnectionError(request=mock.Mock())
        with self.assertRaises(B.SearchUnavailable) as cm:
            run(FakeClient([{"raise_on_enter": err}], clock), clock)
        self.assertEqual(cm.exception.reason, "error")


class Users(unittest.TestCase):
    def test_tow_lookup_stop_caches_nothing(self):
        saved = []
        with mock.patch.object(B, "run_search", side_effect=B.SearchUnavailable("silent", "x: silent twice")), \
             mock.patch.object(T, "allowed_domains", return_value=["mbusa.com"]), \
             mock.patch.object(T, "vehicle_config", return_value=CFG), \
             mock.patch.object(T, "get_cached", return_value=None), \
             mock.patch.object(T, "save", side_effect=lambda *a: saved.append(a)), \
             mock.patch.object(T, "load_tow_overrides", return_value={}):
            out = T.towing_for("2024 Mercedes-Benz GLE", "GLE 450", "SUV", "TRAILER HITCH", force=True)
        self.assertIsNone(out["rating"])
        self.assertIn("tow lookup stopped (silent)", out["note"])
        self.assertEqual(saved, [])

    def test_tow_limits_passed_through(self):
        seen = {}

        def fake(client, **kw):
            seen.update(kw)
            return SimpleNamespace(stop_reason="end_turn", content=[])

        with mock.patch.object(B, "run_search", side_effect=fake), mock.patch.object(T, "allowed_domains", return_value=["x.com"]):
            T.lookup(CFG)
        self.assertEqual((seen["silence_s"], seen["budget_s"], seen["max_requests"]), (120, 480, 4))

    def test_refresh_ignores_the_cache(self):
        calls = []
        with mock.patch.object(T, "vehicle_config", return_value=CFG), \
             mock.patch.object(T, "get_cached", return_value={"tow_rating_lbs": None, "note": "rejected"}), \
             mock.patch.object(T, "lookup", side_effect=lambda c: calls.append(c) or {"lbs": 7700, "url": "u"}), \
             mock.patch.object(T, "save"), mock.patch.object(T, "load_tow_overrides", return_value={}):
            self.assertIsNone(T.towing_for("x", "y", "SUV", "TRAILER HITCH", force=True)["rating"])
            self.assertEqual(T.towing_for("x", "y", "SUV", "TRAILER HITCH", force=True, refresh=True)["rating"], 7700)
        self.assertEqual(len(calls), 1)

    def test_range_lookup_stop_is_unavailable(self):
        import powertrain as P

        with mock.patch.object(B, "run_search", side_effect=B.SearchUnavailable("budget", "over budget")), \
             mock.patch("credentials.ANTHROPIC_API_KEY", "k", create=True):
            with self.assertRaises(P.RangeLookupUnavailable):
                P.manufacturer_search(2023, "Mercedes-Benz", "EQE", "EQE 500", "bev", ["mbusa.com"])

    def test_no_range_data_line_says_state_none(self):
        import powertrain as P

        src = (Path(P.__file__)).read_text(encoding="utf-8")
        self.assertIn("ELECTRIC_RANGE: (omit — range unavailable, state none", src)


class Generate(unittest.TestCase):
    def test_search_stop_falls_back_to_writing_without_search(self):
        import adwriter as A

        created = []

        def create(**kw):
            created.append(kw)
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="<ad>Para one.\n\nTwo.\n\nThree.\n\nFour.</ad>")])

        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        with mock.patch.object(B, "run_search", side_effect=B.SearchUnavailable("silent", "stopped")):
            A.generate_ad(client, "data", "sys", needs_lookup=[{"kind": "feature", "brand": "x", "feature_name": "Burmester"}],
                          stock="X1", make="Mercedes-Benz")
        self.assertEqual(len(created), 1)
        self.assertNotIn("tools", created[0], "the fallback call has no web search tool")
        self.assertNotIn("FEATURES REQUIRING RESEARCH", created[0]["messages"][0]["content"])

    def test_limits(self):
        import adwriter as A

        self.assertEqual((A.GENERATE_SEARCH_SILENCE_S, A.GENERATE_SEARCH_BUDGET_S), (120, 300))
        self.assertFalse(A.GENERATE_SEARCH_ALLOWLIST, "stays off until approved")

    def test_research_instructions_forbid_range_and_tow(self):
        import adwriter as A

        text = A._research_instructions([{"kind": "feature", "brand": "b", "feature_name": "F"}])
        self.assertIn("Never research a towing rating", text)
        self.assertIn("never research an electric range", text)


class BuildPath(unittest.TestCase):
    def test_build_makes_no_live_tow_lookup_and_flags_a_miss(self):
        import adwriter as A

        seen = {}

        def fake_tow(*a, **kw):
            seen.update(kw)
            return {"triggered": True, "rating": None, "config_text": "cfg", "note": "not looked up yet"}

        pkg = {"stock_number": "X1", "vehicle": {}, "msrp_data": {}}
        with mock.patch.object(A, "towing_for", side_effect=fake_tow), \
             mock.patch.object(A, "_snapshot_vehicle", return_value={"vin": "V", "year_make_model": "2024 Mercedes-Benz GLE"}), \
             mock.patch.object(A, "_sticker_text_for", return_value=""), \
             mock.patch.object(A, "cached_texts", return_value=("", "")), \
             mock.patch.object(A, "classify", return_value={"class": "ice"}):
            tow = A.towing_for_package(pkg)
        self.assertIs(seen.get("allow_lookup"), False)
        entry = {}
        A.set_towing_review(entry, tow)
        self.assertIn("not in the tow cache yet", entry["towing_review"])
        self.assertIn("tow_refresh.py --lookup-stocks", entry["towing_review"])


if __name__ == "__main__":
    unittest.main()
