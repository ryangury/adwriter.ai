"""model_config.py: one model flag per call site (all claude-sonnet-4-6 by default), Sonnet 5.5's request fields
(thinking between tools + an effort level; never thinking.disabled, budget_tokens or tool_choice), the caps scaled
for 5.5 with the truncation guard and the double-cap retry kept, and the guard limits. Offline: stub clients."""
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path; temp cost log)

sys.path.insert(0, str(_paths.ROOT))
import adwriter as A  # noqa: E402
import api_cost  # noqa: E402
import model_config as M  # noqa: E402
import towing as T  # noqa: E402


def message(text, stop="end_turn", model="m"):
    return NS(content=[NS(type="text", text=text)], stop_reason=stop, model=model,
              usage=NS(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                       server_tool_use=None))


class Client:
    def __init__(self, replies):
        self.calls, self.replies = [], list(replies)
        self.messages = NS(create=self.create, count_tokens=lambda **k: NS(input_tokens=200))

    def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


class Defaults(unittest.TestCase):
    def test_flags_mb_on_4_6_non_mb_on_5_5(self):
        for name in ("GENERATE_MODEL_MB", "REPRICE_MODEL_MB", "RESEARCH_SEARCH_MODEL_MB", "TOW_LOOKUP_MODEL"):
            self.assertEqual(getattr(M, name), "claude-sonnet-4-6", name)
        for name in ("GENERATE_MODEL_NON_MB", "REPRICE_MODEL_NON_MB", "RESEARCH_SEARCH_MODEL_NON_MB"):
            self.assertEqual(getattr(M, name), "claude-sonnet-5-5", name)
        for status, want in ((10, M.SONNET_46), (16, M.SONNET_46), (None, M.SONNET_46), (99, M.SONNET_46),
                             (11, M.SONNET_55), (12, M.SONNET_55), (13, M.SONNET_55)):
            self.assertEqual((M.generate_model(status), M.reprice_model(status), M.research_search_model(status)),
                             (want, want, want), status)
        self.assertEqual(M.describe(M.tag(M.SONNET_55)), "claude-sonnet-5-5 (effort medium)")
        self.assertEqual(M.tag(M.SONNET_46), {"model": "claude-sonnet-4-6", "effort": None})
        self.assertEqual(M.SONNET_55_EFFORT, "medium")
        self.assertEqual(M.request_kwargs(M.SONNET_46), {"model": "claude-sonnet-4-6"})

    def test_sonnet_5_5_fields(self):
        self.assertEqual(M.request_kwargs(M.SONNET_55),
                         {"model": "claude-sonnet-5-5", "thinking": {"type": "between_tools"}, "output_config": {"effort": "medium"}})
        self.assertEqual(M.request_kwargs(M.SONNET_55, effort="high")["output_config"], {"effort": "high"})
        with self.assertRaises(ValueError):
            M.request_kwargs(M.SONNET_55, effort="max")

    def test_no_forbidden_fields_anywhere(self):
        for f in ("model_config.py", "adwriter.py", "towing.py", "api_cost.py", "bounded_search.py", "powertrain.py"):
            src = (_paths.ROOT / f).read_text(encoding="utf-8")
            for field in ("tool_choice", "budget_tokens"):          # as a request key or a keyword argument
                self.assertIsNone(re.search(rf"""["']{field}["']\s*:|\b{field}\s*=""", src), (f, field))
            self.assertIsNone(re.search(r"""["']type["']\s*:\s*["']disabled["']""", src), f)

    def test_prices(self):
        self.assertEqual(api_cost.PRICES["claude-sonnet-5-5"], (2.00, 10.00))
        self.assertEqual(api_cost.PRICES["claude-sonnet-4-6"], (3.00, 15.00))

    def test_caps_scale_for_5_5(self):
        self.assertEqual(M.output_cap(M.SONNET_46, 2500), 2500)
        self.assertEqual(M.output_cap(M.SONNET_55, 2500), 3000)
        self.assertEqual(M.output_cap(M.SONNET_55, 10000), 12000)
        self.assertEqual((M.INPUT_FACTOR[M.SONNET_55], M.OUTPUT_FACTOR[M.SONNET_55]), (1.49, 1.20))


class Generate(unittest.TestCase):
    AD = "<ad>P1.\n\nP2.\n\nP3.\n\nP4.</ad>"

    def gen(self, model):
        client = Client([message(self.AD)])
        with mock.patch.object(M, "GENERATE_MODEL_MB", model):
            A.generate_ad(client, "data", "system", stock="S1", status_code=10)
        return client.calls[0]

    def test_status_picks_the_flag_and_the_tag_follows(self):
        for status, want in ((10, "claude-sonnet-4-6"), (16, "claude-sonnet-4-6"), (11, "claude-sonnet-5-5"),
                             (12, "claude-sonnet-5-5"), (13, "claude-sonnet-5-5")):
            client = Client([message(self.AD)])
            A.generate_ad(client, "data", "system", stock="T1", status_code=status)
            self.assertEqual(client.calls[0]["model"], want, status)
            self.assertEqual(A.GENERATED_WITH["T1"]["model"], want)

    def test_4_6_request_is_unchanged(self):
        kw = self.gen(M.SONNET_46)
        self.assertEqual((kw["model"], kw["max_tokens"]), ("claude-sonnet-4-6", A.MAX_TOKENS))
        self.assertNotIn("thinking", kw)
        self.assertNotIn("output_config", kw)

    def test_5_5_request(self):
        kw = self.gen(M.SONNET_55)
        self.assertEqual((kw["model"], kw["max_tokens"]), ("claude-sonnet-5-5", 3000))
        self.assertEqual((kw["thinking"], kw["output_config"]), ({"type": "between_tools"}, {"effort": "medium"}))

    def test_search_call_reads_its_own_flag_and_the_guard_its_limits(self):
        seen = {}

        def run(client, *, kwargs, guard=None, **kw):
            seen.update(kwargs=kwargs, guard=guard)
            return message(self.AD)

        with mock.patch("bounded_search.run_search", run), \
             mock.patch.object(A, "_research_instructions", return_value=""), mock.patch.object(A, "web_search_tool_for", return_value={"type": "web_search_20260209", "name": "web_search"}):
            A.generate_ad(Client([]), "data", "system", needs_lookup=[{"feature": "x"}], stock="S1", status_code=12)
        seen_mb = dict(seen)
        with mock.patch("bounded_search.run_search", run), \
             mock.patch.object(A, "_research_instructions", return_value=""), mock.patch.object(A, "web_search_tool_for", return_value={"type": "web_search_20260209", "name": "web_search"}):
            A.generate_ad(Client([]), "data", "system", needs_lookup=[{"feature": "x"}], stock="S1", status_code=10)
        self.assertEqual(seen["kwargs"]["model"], "claude-sonnet-4-6", "status 10: the MB search flag")
        self.assertNotIn("thinking", seen["kwargs"])
        self.assertEqual(seen["kwargs"]["max_tokens"], 3700, "4.6 cap unchanged")
        self.assertEqual((seen["guard"].call_budget, seen["guard"].ad_budget, seen["guard"].max_searches), (0.75, 1.50, 6))
        seen.clear()
        seen.update(seen_mb)
        kw = seen["kwargs"]
        self.assertEqual((kw["model"], kw["max_tokens"], kw["thinking"]["type"]), ("claude-sonnet-5-5", 4440, "between_tools"))
        self.assertEqual((seen["guard"].call_budget, seen["guard"].ad_budget, seen["guard"].max_searches), (0.75, 1.50, 6))

    def test_the_truncation_guard_still_fails_a_cut_off_ad(self):
        client = Client([message("<ad>P1.\n\nP2 cut", stop="max_tokens")])
        with self.assertRaises(ValueError):
            A.generate_ad(client, "data", "system", stock="S1", status_code=13)


class Reprice(unittest.TestCase):
    def test_5_5_cap_retry_at_double_and_fields(self):
        client = Client([message("cut", stop="max_tokens"), message("New paragraph two.")])
        with mock.patch.object(A, "flag_generation_problem"):
            out = A._capped_completion(client, stock="S1", label="reprice", system="s", user="u", size_text="old p2",
                                       floor=A.REPRICE_MIN_TOKENS, status_code=12)
        self.assertEqual(out, "New paragraph two.")
        a, b = client.calls
        self.assertEqual(a["max_tokens"], 1800, "floor 1,500 x 1.2 beats 2 x 200 + 480")
        self.assertEqual(b["max_tokens"], 3600, "the retry is at double the cap")
        self.assertEqual((a["model"], a["thinking"], a["output_config"]), ("claude-sonnet-5-5", {"type": "between_tools"}, {"effort": "medium"}))

    def test_4_6_unchanged_and_still_flags_when_both_attempts_fail(self):
        client = Client([message("x", stop="max_tokens"), message("y", stop="max_tokens")])
        with mock.patch.object(A, "flag_generation_problem") as flag, self.assertRaises(A.TruncatedGenerationError):
            A._capped_completion(client, stock="S1", label="update_recon", system="s", user="u", size_text="p", floor=1500)
        self.assertEqual([c["max_tokens"] for c in client.calls], [1500, 3000])
        self.assertNotIn("thinking", client.calls[0])
        flag.assert_called_once()

    def test_required_sentence_guard_is_untouched(self):
        self.assertIn("missing_required_sentences", Path(A.__file__).read_text(encoding="utf-8"))


class TowLookup(unittest.TestCase):
    def test_tow_lookup_reads_its_flag(self):
        seen = {}

        def run(client, *, kwargs, **kw):
            seen.update(kwargs)
            return NS(content=[NS(type="text", text="TOW :: NONE")], stop_reason="end_turn")

        cfg = {"year": 2024, "make": "Chevrolet", "model": "Silverado 1500", "engine": "3.0L Duramax", "drivetrain": "4WD",
               "cab": "crew", "bed": "short", "truck": True, "tow_package": "", "ev": False}
        with mock.patch.object(M, "TOW_LOOKUP_MODEL", M.SONNET_55), mock.patch("bounded_search.run_search", run), \
             mock.patch.object(api_cost, "make_client", return_value=object()):
            T.lookup(cfg)
        self.assertEqual((seen["model"], seen["max_tokens"], seen["output_config"]["effort"]), ("claude-sonnet-5-5", 19200, "medium"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
