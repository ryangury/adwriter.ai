"""Real-time posting checks: the verifier's due rule, the shared text-change
helper, the blocked-pass guarantee, and the refresh scripts clearing the
verification fields. Offline: synthetic entries, a fake check function, nothing
read from or written to the real ad_history.json."""
import copy
import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import adwriter  # noqa: E402
import carfax_refresh  # noqa: E402
import tow_refresh  # noqa: E402
import verifier  # noqa: E402
import warranty_refresh  # noqa: E402

TODAY = date(2026, 10, 9)


def day(n):
    """n days before TODAY, ISO."""
    return (TODAY - timedelta(days=n)).isoformat()


def confirmed(**kw):
    e = {
        "current_ad_text": "A. B. C.", "first_ad_date": day(10), "last_ad_date": day(10), "ad_count": 1,
        "verification_verdict": "current", "match_score": 99, "identity_confirmed": True,
        "price_mismatch": None, "last_verified": day(1), "verification_note": None,
    }
    e.update(kw)
    return e


class DueRule(unittest.TestCase):
    def test_new_ad_today_is_due(self):
        e = {"current_ad_text": "A.", "first_ad_date": day(0), "last_ad_date": day(0), "ad_count": 1,
             "verification_verdict": None, "last_verified": None}
        self.assertEqual(verifier._due_reason(e, TODAY), "new")

    def test_recon_updated_today_is_due_and_cleared(self):
        e = confirmed(first_ad_date=day(6), last_ad_date=day(6), lifecycle_stage="active")
        self.assertIsNone(verifier._due_reason(e, TODAY))
        changed = adwriter.set_current_ad_text(e, "A. B. C. Four new tires installed.")  # what update_recon does
        e["lifecycle_stage"], e["last_ad_date"] = "recon_updated", TODAY.isoformat()
        self.assertTrue(changed)
        for f in adwriter.VERIFICATION_FIELDS:
            self.assertIsNone(e[f], f)
        self.assertEqual(verifier._due_reason(e, TODAY), "changed")

    def test_same_text_does_not_clear(self):
        e = confirmed()
        self.assertFalse(adwriter.set_current_ad_text(e, "A. B. C."))
        self.assertEqual(e["verification_verdict"], "current")
        self.assertIsNone(verifier._due_reason(e, TODAY))

    def test_outdated_yesterday_is_due(self):
        e = confirmed(verification_verdict="outdated", identity_confirmed=False,
                      first_ad_date=day(2), last_ad_date=day(2), last_verified=day(1))
        self.assertEqual(verifier._due_reason(e, TODAY), "unposted")

    def test_each_unconfirmed_shape_is_due_every_pass_for_three_days(self):
        shapes = {
            "outdated": dict(verification_verdict="outdated"),
            "not_found": dict(verification_verdict="not_found"),
            "not_posted": dict(verification_verdict="not_posted"),
            "identity unconfirmed": dict(identity_confirmed=False),
            "price mismatch": dict(price_mismatch={"live_price": 1, "expected_price": 2}),
        }
        for name, kw in shapes.items():
            for age in (0, 1, 3):
                e = confirmed(last_ad_date=day(age), last_verified=TODAY.isoformat(), **kw)
                self.assertEqual(verifier._due_reason(e, TODAY), "unposted", f"{name} age {age}")
            # past the window it falls back to the drift rotation
            e = confirmed(last_ad_date=day(4), last_verified=TODAY.isoformat(), **kw)
            self.assertIsNone(verifier._due_reason(e, TODAY), f"{name} age 4, verified today")
            e = confirmed(last_ad_date=day(4), last_verified=day(3), **kw)
            self.assertEqual(verifier._due_reason(e, TODAY), "drift", f"{name} age 4, verified 3d ago")

    def test_confirmed_current_checked_yesterday_is_not_due(self):
        self.assertIsNone(verifier._due_reason(confirmed(last_verified=day(1)), TODAY))
        self.assertIsNone(verifier._due_reason(confirmed(last_verified=day(2)), TODAY))

    def test_confirmed_current_drifts_every_three_days(self):
        self.assertEqual(verifier._due_reason(confirmed(last_verified=day(3)), TODAY), "drift")
        # a recent ad that is confirmed has no age gate and no unposted recheck
        self.assertIsNone(verifier._due_reason(confirmed(first_ad_date=day(1), last_ad_date=day(1)), TODAY))

    def test_absent_car_is_not_due(self):
        for e in (
            confirmed(absent_since=day(1), last_verified=day(9)),
            {"current_ad_text": "A.", "first_ad_date": day(0), "verification_verdict": None, "absent_since": day(0)},
            confirmed(verification_verdict="outdated", last_ad_date=day(0), absent_since=day(0)),
        ):
            self.assertIsNone(verifier._due_reason(e, TODAY))

    def test_no_ad_text_is_not_due(self):
        self.assertIsNone(verifier._due_reason({"first_ad_date": day(0), "verification_verdict": None}, TODAY))

    def test_changed_vs_new(self):
        base = {"current_ad_text": "A.", "first_ad_date": day(5), "last_ad_date": day(5), "ad_count": 1,
                "verification_verdict": None}
        self.assertEqual(verifier._due_reason(dict(base), TODAY), "new")
        self.assertEqual(verifier._due_reason({**base, "ad_count": 2}, TODAY), "changed")
        self.assertEqual(verifier._due_reason({**base, "last_ad_date": day(0)}, TODAY), "changed")
        self.assertEqual(verifier._due_reason({**base, "stale_phrases": ["x"]}, TODAY), "changed")


def history():
    return {
        "NEW1": {"current_ad_text": "A.", "first_ad_date": day(0), "last_ad_date": day(0), "ad_count": 1,
                 "verification_verdict": None, "last_verified": None},
        "CHG1": {"current_ad_text": "A.", "first_ad_date": day(8), "last_ad_date": day(0), "ad_count": 2,
                 "lifecycle_stage": "recon_updated", "verification_verdict": None, "last_verified": None},
        "OUT1": confirmed(verification_verdict="outdated", identity_confirmed=False,
                          first_ad_date=day(2), last_ad_date=day(2)),
        "DRF1": confirmed(last_verified=day(4)),
        "OK1": confirmed(last_verified=day(1)),
        "GONE": confirmed(absent_since=day(1), verification_verdict=None, last_verified=None),
    }


def blocked(stock, headless=True):
    return {"blocked": True, "page_found": False}


def not_found(stock, headless=True):
    return {"page_found": False, "url_found": None, "description_text": None}


class Passes(unittest.TestCase):
    def run_pass(self, check, **kw):
        h = history()
        saved = []
        out = io.StringIO()
        with mock.patch.object(adwriter, "save_ad_history", side_effect=lambda x: saved.append(copy.deepcopy(x))), \
             mock.patch.object(verifier, "load_previous_snapshot", return_value=[]), \
             redirect_stdout(out):
            result = verifier.run_verification(h, check=check, today=TODAY, **kw)
        return h, result, out.getvalue()

    def test_blocked_pass_changes_nothing_and_stops_after_three(self):
        before = history()
        calls = []

        def check(stock, headless=True):
            calls.append(stock)
            return blocked(stock)

        h, (cur, post, upd), out = self.run_pass(check)
        self.assertEqual(h, before)
        self.assertEqual((cur, post, upd), ([], [], []))
        self.assertEqual(len(calls), 3)
        self.assertNotIn("GONE", calls)

    def test_pass_logs_counts_by_reason(self):
        _, _, out = self.run_pass(blocked)
        self.assertIn("due: new 1 | changed 1 | unposted recheck 1 | drift 1", out)

    def test_unconfirmed_pass_skips_drift_and_confirmed(self):
        calls = []

        def check(stock, headless=True):
            calls.append(stock)
            return not_found(stock)

        h, _, _ = self.run_pass(check, reasons=verifier.UNCONFIRMED_REASONS)
        self.assertEqual(sorted(calls), ["CHG1", "NEW1", "OUT1"])
        self.assertEqual(h["DRF1"]["last_verified"], day(4))   # untouched
        self.assertEqual(h["OK1"]["last_verified"], day(1))

    def test_full_pass_includes_drift(self):
        calls = []

        def check(stock, headless=True):
            calls.append(stock)
            return not_found(stock)

        self.run_pass(check)
        self.assertEqual(sorted(calls), ["CHG1", "DRF1", "NEW1", "OUT1"])


class RefreshScripts(unittest.TestCase):
    """Each refresh script's apply loop goes through adwriter.set_current_ad_text
    and clear_verification, and shares the one field list."""

    def test_shared_field_list(self):
        for mod in (carfax_refresh, tow_refresh, warranty_refresh):
            self.assertIs(mod.VERIFICATION_FIELDS, adwriter.VERIFICATION_FIELDS, mod.__name__)

    def test_apply_loops_use_the_helper(self):
        for name in ("carfax_refresh.py", "tow_refresh.py", "warranty_refresh.py"):
            src = (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")
            self.assertIn("A.set_current_ad_text(", src, name)
            self.assertIn("A.clear_verification(", src, name)
            self.assertNotIn('e["current_ad_text"] =', src, name)

    def test_edit_clears_fields(self):
        e = confirmed()
        adwriter.set_current_ad_text(e, "A. B.")        # the refresh edit removed a sentence
        adwriter.clear_verification(e)
        for f in adwriter.VERIFICATION_FIELDS:
            self.assertIsNone(e[f], f)
        e["stale_phrases"] = ["C."]                      # what every refresh script also records
        self.assertEqual(verifier._due_reason(e, TODAY), "changed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
