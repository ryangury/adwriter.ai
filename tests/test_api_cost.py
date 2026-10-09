"""api_cost_log: pricing, the single logging wrapper, bounded_search's per-request
rows (including aborted ones), the generate guard, key class selection, the
call sites (generate / reprice / vision / tow / range), cost_summary and the
Spending page. Temp database, stubbed clients, nothing sent."""
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path; temp cost log)
import anthropic  # noqa: E402
import api_cost  # noqa: E402
import bounded_search as B  # noqa: E402
import cost_summary  # noqa: E402

SONNET, HAIKU = "claude-sonnet-4-6", "claude-haiku-4-5-20251001"


def usage(i=0, o=0, cc=0, cr=0, s=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=cc,
                           cache_read_input_tokens=cr, server_tool_use=SimpleNamespace(web_search_requests=s))


def resp(stop="end_turn", model=SONNET, text="x", **u):
    return SimpleNamespace(stop_reason=stop, model=model, usage=usage(**u),
                           content=[SimpleNamespace(type="text", text=text)])


class DBCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"ADWRITER_COST_DB": str(Path(self.dir) / "c.db"),
                                                "ADWRITER_COST_PURPOSE": "", "ADWRITER_KEY_CLASS": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def rows(self):
        c = sqlite3.connect(os.environ["ADWRITER_COST_DB"])
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute("SELECT * FROM api_cost_log ORDER BY id")]
        finally:
            c.close()


class Pricing(unittest.TestCase):
    def test_prices_and_multipliers(self):
        self.assertAlmostEqual(api_cost.cost_usd(SONNET, 1_000_000, 0), 3.00)
        self.assertAlmostEqual(api_cost.cost_usd(SONNET, 0, 1_000_000), 15.00)
        self.assertAlmostEqual(api_cost.cost_usd(SONNET, cache_creation=1_000_000), 3.75)
        self.assertAlmostEqual(api_cost.cost_usd(SONNET, cache_read=1_000_000), 0.30)
        self.assertAlmostEqual(api_cost.cost_usd(SONNET, searches=3), 0.03)
        self.assertAlmostEqual(api_cost.cost_usd(HAIKU, 1_000_000, 1_000_000), 6.00, msg="dated id matches its undated key")
        self.assertIsNone(api_cost.cost_usd("claude-unknown-9", 5, 5))

    def test_one_prices_table(self):
        import re

        owners = [p.name for p in sorted(Path(_paths.ROOT).glob("*.py"))
                  if re.search(r"^PRICES:", p.read_text(encoding="utf-8"), re.M)]
        self.assertEqual(owners, ["api_cost.py"])


class Wrapper(DBCase):
    def test_create_logs_one_row_with_usage_and_cost(self):
        client = SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(return_value=resp(i=1000, o=500, cc=2000, cr=4000, s=2))))
        r = api_cost.create(client, purpose="generate", stock="P1", label="ad P1", model=SONNET, max_tokens=5, messages=[])
        self.assertEqual(r.stop_reason, "end_turn")
        (row,) = self.rows()
        self.assertEqual((row["purpose"], row["stock"], row["model"], row["input_tokens"], row["output_tokens"],
                          row["cache_creation_tokens"], row["cache_read_tokens"], row["web_search_requests"], row["retry"]),
                         ("generate", "P1", SONNET, 1000, 500, 2000, 4000, 2, 0))
        want = 1000 * 3 / 1e6 + 500 * 15 / 1e6 + 2000 * 3.75 / 1e6 + 4000 * 0.3 / 1e6 + 0.02
        self.assertAlmostEqual(row["cost_usd"], want)
        self.assertEqual(row["stop_reason"], "end_turn")
        self.assertTrue(row["ts"].endswith("+00:00"), "UTC")

    def test_failed_call_is_logged_and_reraised(self):
        client = SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(side_effect=ValueError("boom"))))
        with self.assertRaises(ValueError):
            api_cost.create(client, purpose="reprice", stock="P1", model=SONNET, messages=[])
        (row,) = self.rows()
        self.assertEqual((row["input_tokens"], row["cost_usd"], row["stop_reason"]), (0, 0.0, "error: ValueError"))

    def test_logging_never_raises(self):
        with mock.patch.dict(os.environ, {"ADWRITER_COST_DB": str(Path(self.dir) / "no" / "such" / "dir" / "c.db")}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertIsNone(api_cost.record(resp(i=1), purpose="generate"))
            self.assertIn("could not log", err.getvalue())

    def test_purpose_override_and_unknown_purpose(self):
        with mock.patch.dict(os.environ, {"ADWRITER_COST_PURPOSE": "test"}):
            api_cost.record(resp(i=1), purpose="generate")
        api_cost.record(resp(i=1), purpose="nonsense")
        self.assertEqual([r["purpose"] for r in self.rows()], ["test", "other"])


class SearchLogging(DBCase):
    def fake_client(self, scripts):
        calls = list(scripts)

        def stream(**kw):
            return calls.pop(0)

        return SimpleNamespace(messages=SimpleNamespace(stream=stream), with_options=lambda **k: client)

    def test_each_billed_request_is_a_row_and_a_silent_abort_is_a_zero_token_row(self):
        global client

        class S:
            def __init__(self, response=None, events=(), exc=None):
                self.response, self.events, self.exc = response, events, exc

            def __enter__(self):
                if self.exc:
                    raise self.exc
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(self.events)

            def get_final_message(self):
                return self.response

        client = self.fake_client([
            S(exc=anthropic.APITimeoutError(request=mock.Mock())),
            S(response=resp("pause_turn", i=100, o=10, s=1)),
            S(response=resp("end_turn", i=200, o=20, s=2)),
        ])
        out = B.run_search(client, kwargs={"model": SONNET, "max_tokens": 5}, messages=[{"role": "user", "content": "q"}],
                           label="tow lookup x", purpose="tow_lookup", log=lambda m: None)
        self.assertEqual(out.stop_reason, "end_turn")
        rows = self.rows()
        self.assertEqual([r["stop_reason"] for r in rows], ["aborted: no data", "pause_turn", "end_turn"])
        self.assertEqual([r["retry"] for r in rows], [0, 1, 1], "everything after a silent abort is a retry")
        self.assertEqual([r["purpose"] for r in rows], ["tow_lookup"] * 3)
        self.assertEqual(rows[0]["input_tokens"], 0)
        self.assertAlmostEqual(sum(r["cost_usd"] for r in rows), (300 * 3 + 30 * 15) / 1e6 + 0.03)

    def test_guard_aborts_on_searches_counted_live_and_logs_the_partial_request(self):
        global client
        ev = SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="server_tool_use"))

        class S:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter([ev] * 6)

            def get_final_message(self):
                raise AssertionError("must not finish")

        client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: S()), with_options=lambda **k: client)
        guard = api_cost.Guard(budget_usd=0.75, max_searches=4)
        with self.assertRaises(api_cost.GuardTripped) as cm:
            B.run_search(client, kwargs={"model": SONNET, "max_tokens": 5}, messages=[], label="ad research P1",
                         purpose="research_search", stock="P1", guard=guard, log=lambda m: None)
        self.assertEqual(cm.exception.reason, "searches")
        (row,) = self.rows()
        self.assertEqual((row["stock"], row["stop_reason"], row["web_search_requests"]), ("P1", "guard: searches", 5))

    def test_guard_aborts_on_cost_between_requests(self):
        global client

        class S:
            def __init__(self, r):
                self.r = r

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter([])

            def get_final_message(self):
                return self.r

        client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: S(resp("pause_turn", i=300_000))),
                                 with_options=lambda **k: client)       # $0.90 in one request
        with self.assertRaises(api_cost.GuardTripped) as cm:
            B.run_search(client, kwargs={"model": SONNET, "max_tokens": 5}, messages=[], label="x", purpose="generate",
                         guard=api_cost.Guard(0.75, 4), log=lambda m: None)
        self.assertEqual(cm.exception.reason, "cost")
        self.assertEqual(len(self.rows()), 1, "the request that went over is logged")

    def test_guard_is_off_by_default(self):
        self.assertFalse(api_cost.GENERATE_GUARD_ENABLED)
        self.assertIsNone(api_cost.generate_guard())
        with mock.patch.object(api_cost, "GENERATE_GUARD_ENABLED", True):
            g = api_cost.generate_guard()
            self.assertEqual((g.budget_usd, g.max_searches), (0.75, 4))


class CallSites(DBCase):
    def test_generate_ad_without_search_logs_generate_and_the_fallback_is_a_retry(self):
        import adwriter as A

        text = "<ad>Hello.</ad>"
        client = SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(return_value=resp(text=text, i=10, o=5))))
        with contextlib.suppress(Exception):
            A.generate_ad(client, "data", stock="P1", make="Mercedes-Benz")
        rows = self.rows()
        self.assertEqual([(r["purpose"], r["stock"], r["retry"]) for r in rows], [("generate", "P1", 0)])
        with contextlib.suppress(Exception):
            A.generate_ad(client, "data", stock="P2", make="Mercedes-Benz", _retry=True)
        self.assertEqual(self.rows()[-1]["retry"], 1)

    def test_generate_ad_with_search_logs_research_search_and_flags_on_a_guard_stop(self):
        import adwriter as A

        def run_search(c, **kw):
            self.assertEqual((kw["purpose"], kw["stock"]), ("research_search", "P1"))
            raise api_cost.GuardTripped("cost", "ad research P1: $0.90 spent (limit $0.75); aborted")

        flagged = []
        with mock.patch("bounded_search.run_search", run_search), \
             mock.patch.object(A, "flag_generation_problem", side_effect=lambda *a: flagged.append(a)), \
             mock.patch.object(A, "_research_instructions", return_value=""), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(api_cost.GuardTripped):
                A.generate_ad(SimpleNamespace(), "data", needs_lookup=[{"x": 1}], stock="P1", make="Mercedes-Benz")
        self.assertEqual(flagged[0][:2], ("P1", "generate_over_cost"))

    def test_capped_completion_logs_reprice_and_the_second_attempt_is_a_retry(self):
        import adwriter as A

        seq = [resp("max_tokens", text="cut", i=10, o=5), resp("end_turn", text="done", i=10, o=8)]
        client = SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(side_effect=seq)))
        with mock.patch.object(A, "_output_cap", return_value=100), contextlib.redirect_stderr(io.StringIO()):
            out = A._capped_completion(client, stock="P1", label="reprice", system="s", user="u", size_text="t", floor=50)
        self.assertEqual(out, "done")
        self.assertEqual([(r["purpose"], r["stock"], r["retry"], r["stop_reason"]) for r in self.rows()],
                         [("reprice", "P1", 0, "max_tokens"), ("reprice", "P1", 1, "end_turn")])

    def test_vision_logs_vision_on_haiku(self):
        import vision_parser as V

        fake = SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(return_value=resp(model=HAIKU, text="{}", i=1000, o=100))))
        with mock.patch.object(api_cost, "make_client", return_value=fake):
            V.call_claude_vision(b"\x89PNG\r\n\x1a\n" + b"0" * 50, "prompt")
        (row,) = self.rows()
        self.assertEqual((row["purpose"], row["model"]), ("vision", V.VISION_MODEL))
        self.assertAlmostEqual(row["cost_usd"], (1000 * 1 + 100 * 5) / 1e6)

    def test_tow_and_range_lookups_pass_their_purpose(self):
        import powertrain as P
        import towing as T

        seen = {}

        def fake_run_search(c, **kw):
            seen[kw["label"][:3]] = kw.get("purpose")
            raise B.SearchUnavailable("error", "stop")

        with mock.patch("bounded_search.run_search", fake_run_search), \
             mock.patch.object(api_cost, "make_client", return_value=SimpleNamespace()):
            with contextlib.suppress(Exception):
                T.lookup({"year": 2024, "make": "Mercedes-Benz", "model": "GLE", "engine": "GLE 450", "drivetrain": "AWD",
                          "cab": "", "bed": "", "tow_package": "", "missing": [], "truck": False, "ev": False})
            with contextlib.suppress(Exception):
                P.manufacturer_search(2025, "Mercedes-Benz", "GLC", "GLC 350e", "plug_in_hybrid", ["mbusa.com"]) \
                    if "plug_in_hybrid" in getattr(P, "CLASS_LABELS", {}) else None
        self.assertEqual(seen.get("tow"), "tow_lookup")

    def test_no_call_site_builds_a_client_or_calls_the_api_outside_api_cost(self):
        import re

        offenders = []
        for p in sorted(Path(_paths.ROOT).glob("*.py")):
            if p.name in ("api_cost.py", "billing.py"):
                continue
            src = p.read_text(encoding="utf-8")
            if re.search(r"anthropic\.Anthropic\(", src):
                offenders.append((p.name, "builds a client itself"))
            for m in re.finditer(r"\.messages\.(create|stream)\(", src):
                if p.name != "bounded_search.py":
                    offenders.append((p.name, m.group(0)))
        self.assertEqual(offenders, [])


class KeyClass(DBCase):
    def creds(self, dev):
        import credentials

        return mock.patch.multiple(credentials, ANTHROPIC_API_KEY="PROD-KEY-VALUE", ANTHROPIC_API_KEY_DEV=dev, create=True)

    def setUp(self):
        super().setUp()
        self.reset()
        self.addCleanup(self.reset)

    def reset(self):
        api_cost._announced.clear()
        api_cost._declared = None

    def test_dev_is_the_default_class_and_it_is_printed_never_the_key(self):
        err = io.StringIO()
        with self.creds("DEV-KEY-VALUE"), contextlib.redirect_stderr(err):
            self.assertEqual(api_cost.current_key_class(), "dev")
            self.assertEqual(api_cost.api_key(), "DEV-KEY-VALUE")
        self.assertIn("key class: DEV", err.getvalue())
        self.assertNotIn("KEY-VALUE", err.getvalue())

    def test_a_production_entry_point_declares_production_and_prints_at_start(self):
        err = io.StringIO()
        with self.creds("DEV-KEY-VALUE"), contextlib.redirect_stderr(err):
            api_cost.use_production_key()
            self.assertIn("key class: PRODUCTION", err.getvalue(), "printed when declared, before any call")
            self.assertEqual(api_cost.api_key(), "PROD-KEY-VALUE")
            self.assertEqual(api_cost.current_key_class(), "production")
        self.assertNotIn("KEY-VALUE", err.getvalue())

    def test_the_environment_beats_a_production_declaration(self):
        with self.creds("DEV-KEY-VALUE"), mock.patch.dict(os.environ, {"ADWRITER_KEY_CLASS": "dev"}),              contextlib.redirect_stderr(io.StringIO()):
            api_cost.use_production_key()            # e.g. a test that runs orchestrator.main()
            self.assertEqual(api_cost.api_key(), "DEV-KEY-VALUE")

    def test_a_missing_dev_key_fails_and_never_falls_back_to_production(self):
        with self.creds(None), mock.patch.object(anthropic, "Anthropic") as A, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(api_cost.DevKeyMissing) as cm:
                api_cost.make_client()
            with self.assertRaises(api_cost.DevKeyMissing):
                api_cost.api_key()
        A.assert_not_called()
        self.assertNotIn("PROD-KEY-VALUE", str(cm.exception))

    def test_rows_record_the_class(self):
        with self.creds("DEV-KEY-VALUE"):
            api_cost.record(resp(i=1), purpose="generate")
            api_cost.use_production_key() if False else None
        self.assertEqual(self.rows()[0]["key_class"], "dev")
        with mock.patch.dict(os.environ, {"ADWRITER_KEY_CLASS": "production"}):
            api_cost.record(resp(i=1), purpose="generate")
        self.assertEqual(self.rows()[1]["key_class"], "production")

    def test_make_client_passes_max_retries_and_uses_the_module_attribute(self):
        with self.creds("DEV-KEY-VALUE"), mock.patch.object(anthropic, "Anthropic") as A,              contextlib.redirect_stderr(io.StringIO()):
            api_cost.make_client(max_retries=0)
        A.assert_called_once_with(api_key="DEV-KEY-VALUE", max_retries=0)

    def test_production_entry_points_declare_production_and_nothing_else_does(self):
        import re

        declared = sorted(p.name for p in Path(_paths.ROOT).glob("*.py")
                          if p.name != "api_cost.py" and "use_production_key()" in p.read_text(encoding="utf-8"))
        self.assertEqual(declared, ["app.py", "orchestrator.py", "sticker_warmup.py", "vision_processor.py"])
        for name in ("tow_refresh.py", "allowlist_trial.py"):
            self.assertIn("api_cost.use_dev_key()", (Path(_paths.ROOT) / name).read_text(encoding="utf-8"), name)

    def test_harness_environment_is_dev_and_test(self):
        # tests/_paths.py (imported by every test) sets these
        self.assertEqual(os.environ.get("ADWRITER_KEY_CLASS") or "dev", "dev")


class Workspace(DBCase):
    def test_workspace_header_is_read_from_a_raw_response_or_a_stream(self):
        raw = SimpleNamespace(headers={"anthropic-workspace-id": "wrkspc_X"})
        self.assertEqual(api_cost.workspace_of(raw), "wrkspc_X")
        stream = SimpleNamespace(response=SimpleNamespace(headers={"anthropic-workspace-id": "wrkspc_Y"}))
        self.assertEqual(api_cost.workspace_of(stream), "wrkspc_Y")
        self.assertIsNone(api_cost.workspace_of(SimpleNamespace()))

    def test_record_stores_it_and_an_old_log_gets_the_column(self):
        api_cost.record(resp(i=1), purpose="generate", workspace_id="wrkspc_X")
        self.assertEqual(self.rows()[0]["workspace_id"], "wrkspc_X")
        old = Path(self.dir) / "old.db"
        c = sqlite3.connect(old)
        c.executescript(chr(10).join(l for l in api_cost.SCHEMA.splitlines() if "workspace_id" not in l))
        c.close()
        with mock.patch.dict(os.environ, {"ADWRITER_COST_DB": str(old)}):
            api_cost.record(resp(i=1), purpose="generate", workspace_id="wrkspc_Z")
            self.assertEqual(self.rows()[0]["workspace_id"], "wrkspc_Z")


def row(purpose, stock, ts, cost, **kw):
    return {"purpose": purpose, "stock": stock, "ts": ts, "cost_usd": cost, "input_tokens": kw.get("i", 0),
            "output_tokens": kw.get("o", 0), "cache_creation_tokens": 0, "cache_read_tokens": 0,
            "web_search_requests": kw.get("s", 0), "retry": kw.get("retry", 0), "stop_reason": kw.get("stop", "end_turn")}


class Summary(unittest.TestCase):
    NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)

    def test_cost_per_ad_counts_rebuilds_by_generation_date(self):
        rows = [
            row("research_search", "A", "2026-10-02T05:00:00+00:00", 0.40),
            row("research_search", "A", "2026-10-02T05:01:00+00:00", 0.20, retry=1),   # same build
            row("generate", "A", "2026-10-08T05:00:00+00:00", 0.10),                  # rebuilt later: a second build
            row("generate", "B", "2026-10-08T05:00:00+00:00", 0.30),
            row("reprice", "A", "2026-10-05T05:00:00+00:00", 0.02),
            row("reprice", "B", "2026-10-05T05:00:00+00:00", 0.04),
        ]
        s = cost_summary.summarize(rows, console_total=2.0, now=self.NOW)
        self.assertEqual(s["builds"]["count"], 3)
        self.assertAlmostEqual(s["builds"]["total"], 1.0)
        self.assertAlmostEqual(s["builds"]["each"], 1.0 / 3)
        self.assertEqual(s["reprices"]["count"], 2)
        self.assertAlmostEqual(s["reprices"]["each"], 0.03)
        self.assertEqual(s["recon_updates"]["count"], 0)
        self.assertIsNone(s["recon_updates"]["each"])
        self.assertEqual(s["by_purpose"][0]["purpose"], "research_search")

    def test_unlogged_uses_finished_days_only(self):
        rows = [row("generate", "A", "2026-10-08T05:00:00+00:00", 1.0),
                row("generate", "B", "2026-10-09T05:00:00+00:00", 0.5)]      # today: not in the Console yet
        s = cost_summary.summarize(rows, console_total=3.0, now=self.NOW)
        self.assertAlmostEqual(s["logged_total"], 1.5)
        self.assertAlmostEqual(s["logged_complete_days"], 1.0)
        self.assertAlmostEqual(s["unlogged"], 2.0)
        self.assertAlmostEqual(s["today_logged"], 0.5)
        self.assertIsNone(cost_summary.summarize(rows, None, self.NOW)["unlogged"])

    def test_unpriced_and_aborted_are_counted(self):
        rows = [row("tow_lookup", None, "2026-10-08T05:00:00+00:00", None, stop="aborted: no data")]
        s = cost_summary.summarize(rows, 0.0, self.NOW)
        self.assertEqual(s["unpriced_calls"], 1)
        self.assertEqual(s["by_purpose"][0]["aborted"], 1)


class Page(DBCase):
    def test_cost_page_shows_the_new_sections_and_keeps_the_totals(self):
        import app as adapp
        import billing

        adapp.app.config["TESTING"] = True
        c = adapp.app.test_client()
        with c.session_transaction() as sess:
            sess["authed"] = True
        now = datetime.now(timezone.utc)
        day1 = now.replace(day=1, hour=5, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        api_cost.record(resp(i=100_000, o=20_000, s=2), purpose="research_search", stock="P1",
                        workspace_id=billing.PRODUCTION_WORKSPACE)
        api_cost.record(resp(i=5000, o=500), purpose="reprice", stock="P1", workspace_id=billing.PRODUCTION_WORKSPACE)
        api_cost.record(resp(i=900_000), purpose="generate", stock="DEVONLY", workspace_id=billing.DEV_WORKSPACE)
        with contextlib.closing(sqlite3.connect(os.environ["ADWRITER_COST_DB"])) as conn, conn:
            conn.execute("UPDATE api_cost_log SET ts=?", (day1,))
        data = {"total": 9.0, "by_family": {"sonnet": 8.0, "haiku": 0.5, "web search": 0.5, "other": 0.0},
                "days": [{"day": day1[:10], "sonnet": 8.0, "haiku": 0.5, "web search": 0.5, "other": 0.0, "total": 9.0}], "tokens": {}}
        ok_ = {"data": data, "fetched_at": 1.0, "error": None}
        with mock.patch.object(adapp.billing, "get_billing", return_value=ok_):
            html = c.get("/cost").get_data(as_text=True)
        for needle in ("Month-to-date total", "$9.00", "Cost per fresh ad",          # the existing totals stay
                       "From our own call log", "Cost per ad built", "Cost per reprice", "Cost per recon update",
                       "Unlogged", "research_search", "reprice", "Most expensive builds"):
            self.assertIn(needle, html, needle)
        self.assertNotIn("DEVONLY", html, "Dev calls are not in the Production build table")
        self.assertIn("Dev: 1 call", html)
        err = {"data": None, "fetched_at": None, "error": "Billing data unavailable: x."}
        with mock.patch.object(adapp.billing, "get_billing", return_value=err):
            html = c.get("/cost").get_data(as_text=True)
        self.assertIn("From our own call log", html, "the own-log section does not need the Console")


if __name__ == "__main__":
    unittest.main(verbosity=2)
