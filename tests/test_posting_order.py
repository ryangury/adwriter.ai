"""Full orchestrator.run() with the existing stubs: the step order is
build, Build Summary, posting check 1 (unconfirmed ads only), CTR, benchmark,
posting check 2, Action Required - and a hung benchmark, a blocked first
posting check, an exception in the first posting check and a clean run all
still send Build Summary and Action Required, and none sends Ads Ready or a
posting alert (email_config flags are off). With a flag on, its email is back. Nothing real is
touched: crawl, files, browsers, Claude and email are stubbed; the "blocked"
case runs the REAL verifier.run_verification against a fake check."""
import contextlib
import io
import sys
import unittest
from datetime import date
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path)
import adwriter  # noqa: E402
import orchestrator as o  # noqa: E402
import verifier  # noqa: E402

sys.path.insert(0, __import__("os").path.dirname(__file__))
from _children import fake_recorded_today, fake_run_watched, ok_children  # noqa: E402
from test_per_ad_email import AD, FEEDBACK, PKG  # noqa: E402

VEH = [{"stock_number": "CT23308A", "status_code": 11, "vin": "V", "year_make_model": "2024 Chevrolet Silverado 1500"}]
TODAY = date.today()


def row(stock):
    return {"stock_number": stock, "year_make_model": "x", "entry": {}, "check": {"url_found": None},
            "compare": {"verdict": "not_found", "match_score": 0, "missing_phrases": []}, "days_since_ad": 1}


# Action Required is only sent when there is something to action (orchestrator:
# "nothing to action - not sent"), so every run carries a pre-recon ad that
# is being watched, as a real morning does.
PRE_RECON = {"current_ad_text": "A.", "first_ad_date": "2026-10-01", "last_ad_date": "2026-10-01", "ad_count": 1,
             "recon_pending": True, "verification_verdict": "current", "identity_confirmed": True,
             "price_mismatch": None, "last_verified": TODAY.isoformat()}


def stored_history():
    """One unconfirmed ad (no verdict), one confirmed ad checked today, and the
    pre-recon ad."""
    return {
        "PRE1": dict(PRE_RECON),
        "UNCONF": {"current_ad_text": "A.", "first_ad_date": TODAY.isoformat(), "last_ad_date": TODAY.isoformat(),
                   "ad_count": 1, "verification_verdict": None, "last_verified": None},
        "SURE": {"current_ad_text": "A.", "first_ad_date": "2026-01-01", "last_ad_date": "2026-01-01", "ad_count": 1,
                 "verification_verdict": "current", "identity_confirmed": True, "price_mismatch": None,
                 "last_verified": TODAY.isoformat()},
    }


class Fake:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a, **k):
        pass

    def require_durham(self, c):
        return "Mercedes-Benz of Durham"


class Run:
    """verify_effects: one entry per posting check, each a callable(history, **kw)
    returning (current, needs_posting, needs_update) or raising."""

    def __init__(self, verify_effects, child_status=None, history=None, ads_ready=False, posting_alert=False):
        self.events = []
        self.verify_kwargs = []
        self.alerts = []
        effects = list(verify_effects)
        hist = history if history is not None else {"PRE1": dict(PRE_RECON)}
        ev = self.events

        def verify(h, **kw):
            n = len(self.verify_kwargs) + 1
            self.verify_kwargs.append(kw)
            ev.append(f"verify{n}")
            return effects[n - 1](h, **kw)

        def on_child(kind, store):
            ev.append(f"child:{kind}")
            if kind == "benchmark" and child_status:
                return child_status, {"counts": {}, "errors": [], "aborted": None}
            return ok_children(kind, store)

        def send(subject, body):
            ev.append(subject)
            self.sent.append((subject, body))

        self.sent = []
        self.patches = [
            mock.patch.object(o, "acquire_scraper_lock"), mock.patch.object(o, "release_lock_if_owned", return_value=True),
            mock.patch.object(o, "load_previous_snapshot", return_value=None),
            mock.patch.object(o, "crawl_inventory", return_value=VEH),
            mock.patch.object(o, "prune_sticker_cache", return_value=0),
            mock.patch.object(o, "flag_absent_ad_history", return_value=([], [])),
            mock.patch.object(o, "save_snapshot"), mock.patch.object(o, "stamp_eligible", return_value=[]),
            mock.patch.object(o, "load_ad_history", side_effect=lambda: dict(hist)),
            mock.patch.object(o, "save_ad_history"),
            mock.patch.object(o, "detect_reprices_needed", return_value=[]),
            mock.patch.object(o, "_load_reprice_queue", return_value=[]), mock.patch.object(o, "_save_reprice_queue"),
            mock.patch.object(o, "ReconVisionScraper", Fake),
            mock.patch.object(o, "check_recon", return_value={"recon_complete": True}),
            mock.patch.object(o, "aggregate", return_value=dict(PKG, recon_complete=True)),
            mock.patch.object(o, "source_status", return_value={}),
            mock.patch.object(o, "_generate_from_package", return_value=(AD, FEEDBACK)),
            mock.patch.object(o, "record_ad"), mock.patch.object(o, "ACVMaxScraper", Fake),
            mock.patch.object(o, "recorded_today", side_effect=fake_recorded_today()),
            mock.patch.object(o, "run_watched", side_effect=fake_run_watched(on_child)),
            mock.patch.object(o, "run_verification", side_effect=verify),
            mock.patch.object(o, "_carfax_leftovers", return_value=[]),
            mock.patch.object(o.list_not_rebuilt, "report", return_value=[]),
            mock.patch.object(o, "send_verification_alert", side_effect=lambda *a, **k: self.alerts.append(a)),
            mock.patch.object(o, "HendrickCarsScraper", side_effect=RuntimeError("no browser in tests")),
            mock.patch.object(o, "_send_gmail", side_effect=send),
            mock.patch.object(o, "EMAIL_ADS_READY", ads_ready),
            mock.patch.object(o, "EMAIL_POSTING_ALERT", posting_alert),
        ]

    def run(self):
        out = io.StringIO()
        with contextlib.ExitStack() as st:
            for p in self.patches:
                st.enter_context(p)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = o.run()
        self.output = out.getvalue()
        return rc

    def idx(self, prefix):
        hits = [i for i, e in enumerate(self.events) if e.startswith(prefix)]
        assert hits, f"{prefix!r} not in {self.events}"
        return hits

    def sent_subject(self, part):
        return [s for s, _ in self.sent if part in s]


def clean(h, **kw):
    return [], [], []


def boom(h, **kw):
    raise RuntimeError("verifier crashed")


def real_blocked(h, **kw):
    """The real run_verification against a fake check that is always blocked."""
    with mock.patch.object(adwriter, "save_ad_history"), \
         mock.patch.object(verifier, "load_previous_snapshot", return_value=[]):
        return verifier.run_verification(h, check=lambda stock, headless=True: {"blocked": True}, **kw)


class StepOrder(unittest.TestCase):
    def assert_still_sends(self, r, rc):
        self.assertEqual(rc, 0)
        self.assertEqual(r.sent_subject("Ads Ready"), [], r.events)
        self.assertEqual(r.alerts, [], "no posting alert")
        self.assertEqual(r.sent_subject("Posting Alert"), [], r.events)
        self.assertNotIn("HENDRICKCARS.COM LINKS", r.output, "the link lookup only feeds Ads Ready")
        self.assertEqual(len(r.sent_subject("Action Required")), 1, r.events)
        self.assertEqual(len(r.sent_subject("Build Summary")), 1, r.events)

    def assert_order(self, r):
        e = r.events
        build, v1 = r.idx("Mercedes-Benz of Durham — Build Summary")[0], r.idx("verify")[0]
        durham = r.idx("child:durham")[0]
        bench = r.idx("child:benchmark")
        v2 = r.idx("verify")[-1]
        action = r.idx("Mercedes-Benz of Durham — Action Required")[0]
        self.assertLess(r.idx("Ad Ready — CT23308A")[0], build, "per-ad email goes out as the ad is built")
        self.assertLess(build, v1, e)
        self.assertLess(v1, durham, e)
        self.assertLess(durham, bench[0], e)
        self.assertLess(bench[-1], v2, e)
        self.assertLess(v2, action, e)
        self.assertEqual(len(r.idx("verify")), 2, "exactly two posting checks")
        print("\n   order:", " > ".join(x.replace("Mercedes-Benz of Durham — ", "") for x in e))

    def test_clean_run(self):
        r = Run([clean, clean])
        rc = r.run()
        self.assert_still_sends(r, rc)
        self.assert_order(r)
        for kw in r.verify_kwargs:
            self.assertEqual(kw["reasons"], verifier.UNCONFIRMED_REASONS, "posting checks cover only unconfirmed ads")

    def test_hung_benchmark(self):
        r = Run([clean, clean], child_status="hung")
        rc = r.run()
        self.assert_still_sends(r, rc)
        self.assert_order(r)
        self.assertEqual(len(r.sent_subject("benchmark hung")), 1, "watchdog email kept")

    def test_blocked_first_posting_check(self):
        r = Run([real_blocked, clean], history=stored_history())
        rc = r.run()
        self.assert_still_sends(r, rc)
        self.assert_order(r)
        self.assertIn("blocked (Akamai)", r.output)
        self.assertIn("due: new 1 | changed 0 | unposted recheck 0 | drift 0", r.output)
        self.assertNotIn("posting check (after build) failed", r.output, "a blocked pass is not an error")

    def test_blocked_both_checks(self):
        r = Run([real_blocked, real_blocked], history=stored_history())
        rc = r.run()
        self.assert_still_sends(r, rc)
        self.assert_order(r)
        self.assertEqual(r.output.count("blocked (Akamai)"), 2)

    def test_exception_in_first_posting_check(self):
        r = Run([boom, clean])
        rc = r.run()
        self.assert_still_sends(r, rc)
        self.assert_order(r)
        body = dict(r.sent)[r.sent_subject("Action Required")[0]]
        self.assertIn("posting check (after build)", body, "the failure is reported, not hidden")
        self.assertIn("verifier crashed", body)

    def test_exception_in_both_posting_checks(self):
        r = Run([boom, boom])
        rc = r.run()
        self.assert_still_sends(r, rc)
        self.assert_order(r)

    def test_second_check_answer_replaces_the_first(self):
        # posted between the checks: check 1 said not found, check 2 says current
        r = Run([lambda h, **k: ([], [row("X1")], []), lambda h, **k: ([row("X1")], [], [])])
        self.assert_still_sends(r, r.run())

    def test_flags_turn_the_emails_back_on(self):
        r = Run([lambda h, **k: ([], [row("X1")], []), clean], ads_ready=True, posting_alert=True)
        self.assertEqual(r.run(), 0)
        self.assertEqual(len(r.sent_subject("Ads Ready")), 1, r.events)
        self.assertEqual(len(r.alerts), 1)
        e = r.events
        self.assertLess(r.idx("verify")[0], r.idx("Mercedes-Benz of Durham — Ads Ready")[0], "Ads Ready after posting check 1")
        self.assertLess(r.idx("Mercedes-Benz of Durham — Ads Ready")[0], r.idx("child:durham")[0], e)

    def test_still_unposted_is_alerted_and_survives_a_blocked_second_check(self):
        r = Run([lambda h, **k: ([], [row("X1")], []), clean], posting_alert=True)
        self.assertEqual(r.run(), 0)
        self.assertEqual([[x["stock_number"] for x in a[0]] for a in r.alerts], [["X1"]])
        r = Run([lambda h, **k: ([], [row("X1")], []), lambda h, **k: ([], [row("X1")], [])], posting_alert=True)
        r.run()
        self.assertEqual([[x["stock_number"] for x in a[0]] for a in r.alerts], [["X1"]], "no duplicate row")


class VerifierAlertFlag(unittest.TestCase):
    def test_send_verification_alert_respects_the_flag(self):
        import email_config
        sent = []
        with mock.patch.object(adwriter, "_send_gmail", side_effect=lambda s, b: sent.append(s)):
            with mock.patch.object(email_config, "EMAIL_POSTING_ALERT", False), contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(verifier.send_verification_alert([row("X1")], [], send=True))
            self.assertEqual(sent, [], "flag off: nothing sent, even with send=True")
            with mock.patch.object(email_config, "EMAIL_POSTING_ALERT", True):
                body = verifier.send_verification_alert([row("X1")], [], send=True)
            self.assertIn("X1", body)
            self.assertEqual(len(sent), 1)

    def test_flags_default_off(self):
        import email_config
        self.assertIs(email_config.EMAIL_ADS_READY, False)
        self.assertIs(email_config.EMAIL_POSTING_ALERT, False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
