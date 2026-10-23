"""The morning run's research refresh step (orchestrator.research_refresh_step / research.auto_refresh), inside the
stub full run of test_posting_order: an entry refreshes the text and sends a "Text Refresh" email and a TEXT
REFRESHED section; an ad already refreshed is left alone; a car built this run and an absent car are skipped; a
per-car error goes to Action Required and the run continues; a hung benchmark and a blocked posting check still send
Build Summary and Action Required; the website's Refresh button refuses while ANOTHER process holds the lock, and
apply_refresh proceeds when the calling process holds it. Nothing real is touched."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path)
import model_config  # noqa: E402
import orchestrator as o  # noqa: E402
import research  # noqa: E402
import run_lock  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from test_posting_order import PRE_RECON, VEH, Run, clean, real_blocked  # noqa: E402

AD = "R1 is ready.\n\nPower comes from a 3.0L six. Ships free.\n\nWarranty.\n\nCall us."
NEW = AD.replace("3.0L six.", "3.0L six. It is rated to tow up to 7,700 lbs.")
CARS = VEH + [{"stock_number": s, "status_code": 10, "vin": "V" + s, "year_make_model": "2024 Mercedes-Benz GLS",
               "current_price": 68875.0} for s in ("R1", "R2", "R3")]
DECISION = {"tow": {"how": "Research entry", "figure": 7700, "source_url": "https://www.mbusa.com/gls", "date": "2026-10-23",
                    "config_text": "2024 Mercedes-Benz GLS GLS 450 AWD [tow equipment: TRAILER HITCH]"}}
KEYS = ("paragraph_one", "paragraph_two", "paragraph_three", "paragraph_four")


def split(t):
    return dict(zip(KEYS, t.split("\n\n")))


def plan_for(stock, changes=True):
    return {"stock": stock, "before": split(AD), "after": split(NEW if changes else AD), "removed": [],
            "notes": ["tow: add -> It is rated to tow up to 7,700 lbs."] if changes else ["tow: already states 7,700 lbs"],
            "changes": changes, "tow": {}, "pt": {}}


def history(**extra):
    h = {"PRE1": dict(PRE_RECON),
         "R1": {"current_ad_text": AD, "first_ad_date": "2026-10-01", "last_ad_date": "2026-10-01", "ad_count": 1}}
    h.update(extra)
    return h


class Step:
    """Run + the step's own stubs. applied: stocks apply_refresh was called for."""

    def __init__(self, hist, *, changes=True, fail=(), verify=None, child_status=None):
        self.applied = []
        self.r = Run(verify or [clean, clean], child_status=child_status, history=hist)
        tmp = Path(tempfile.mkdtemp(prefix="research_refresh_"))

        def apply(stock, plan=None, backup=True):
            if stock in fail:
                raise RuntimeError(f"disk full for {stock}")
            self.applied.append((stock, backup))
            return dict(plan, applied=True)

        def record(h, stock, **kw):         # the build stores CT23308A's new ad, as a real run does
            hist[stock] = {"current_ad_text": kw.get("ad_text") or AD, "first_ad_date": "2026-10-23", "ad_count": 1}

        self.r.patches += [
            mock.patch.object(model_config, "RESEARCH_AUTO_APPLY", True),     # off by default until approved
            mock.patch.object(o, "record_ad", side_effect=record),
            mock.patch.object(o, "crawl_inventory", return_value=CARS),
            mock.patch.object(research, "refresh_decisions", return_value=DECISION),
            mock.patch.object(research, "plan_refresh", side_effect=lambda s, **k: plan_for(s, changes)),
            mock.patch.object(research, "apply_refresh", side_effect=apply),
            mock.patch.object(research.shutil, "copy2", side_effect=lambda *a: None),
            mock.patch("adwriter.AD_HISTORY_PATH", tmp / "ad_history.json"),
        ]

    def run(self):
        return self.r.run()

    def summary(self):
        return dict(self.r.sent)[self.r.sent_subject("Build Summary")[0]]


class FullRun(unittest.TestCase):
    def test_entry_refreshes_the_text_and_sends_an_email(self):
        s = Step(history())
        self.assertEqual(s.run(), 0)
        self.assertEqual(s.applied, [("R1", False)], "one backup per run, not per car")
        (subj,) = s.r.sent_subject("Text Refresh")
        self.assertEqual(subj, "Text Refresh — R1 — 2024 Mercedes-Benz GLS — $69,774")
        body = dict(s.r.sent)[subj]
        self.assertIn('added:    "It is rated to tow up to 7,700 lbs."', body)
        self.assertIn("source https://www.mbusa.com/gls, entered 2026-10-23", body)
        self.assertIn("configuration: 2024 Mercedes-Benz GLS GLS 450 AWD", body)
        summary = s.summary()
        self.assertIn("TEXT REFRESHED (Research entries, VIN overrides, No figure needed; no model call) (1)", summary)
        self.assertIn("[R1]  2024 Mercedes-Benz GLS", summary)
        self.assertLess(s.r.idx("Text Refresh")[0], s.r.idx("Mercedes-Benz of Durham — Build Summary")[0])

    def test_already_refreshed_changes_nothing(self):
        s = Step(history(), changes=False)
        s.run()
        self.assertEqual(s.applied, [])
        self.assertEqual(s.r.sent_subject("Text Refresh"), [])
        self.assertIn("TEXT REFRESHED (Research entries, VIN overrides, No figure needed; no model call) (0)", s.summary())

    def test_a_car_built_this_run_is_skipped(self):
        s = Step(history())
        s.run()
        self.assertNotIn("CT23308A", [a for a, _ in s.applied], "CT23308A was built this run")
        self.assertIn("built this run", s.r.output)

    def test_an_absent_car_is_skipped(self):
        s = Step(history(R2={"current_ad_text": AD, "absent_since": "2026-10-22"}))
        s.run()
        self.assertEqual([a for a, _ in s.applied], ["R1"])

    def test_an_error_goes_to_action_required_and_the_run_continues(self):
        s = Step(history(R3={"current_ad_text": AD, "first_ad_date": "2026-10-01"}), fail={"R1"})
        self.assertEqual(s.run(), 0)
        self.assertEqual([a for a, _ in s.applied], ["R3"], "R3 is still refreshed")
        action = dict(s.r.sent)[s.r.sent_subject("Action Required")[0]]
        self.assertIn("disk full for R1", action)
        self.assertEqual(len(s.r.sent_subject("Build Summary")), 1)

    def test_hung_benchmark_and_blocked_posting_check_still_send_both(self):
        from test_posting_order import stored_history

        hist = dict(stored_history(), **{k: v for k, v in history().items() if k != "PRE1"})
        s = Step(hist, verify=[real_blocked, clean], child_status="hung")
        self.assertEqual(s.run(), 0)
        self.assertEqual(len(s.r.sent_subject("Build Summary")), 1)
        self.assertEqual(len(s.r.sent_subject("Action Required")), 1)
        self.assertEqual(len(s.r.sent_subject("Text Refresh")), 1)

    def test_first_week_cap_leaves_the_rest_for_the_next_run(self):
        self.assertEqual(model_config.RESEARCH_AUTO_APPLY_MAX_CARS, 5)
        extra = {f"R{i}": {"current_ad_text": AD, "first_ad_date": "2026-10-01"} for i in range(4, 11)}
        global CARS
        cars = CARS + [{"stock_number": s, "status_code": 10, "vin": "V" + s, "year_make_model": "2024 Mercedes-Benz GLS",
                        "current_price": 68875.0} for s in extra]
        s = Step(history(**extra))
        s.r.patches.append(mock.patch.object(o, "crawl_inventory", return_value=cars))
        s.run()
        self.assertEqual(len(s.applied), 5)
        self.assertEqual(len(s.r.sent_subject("Text Refresh")), 5)
        self.assertIn("left for the next run (5 per run)", s.summary())

    def test_flag_off_skips_the_step(self):
        self.assertFalse(model_config.RESEARCH_AUTO_APPLY, "off until approved")
        s = Step(history())
        s.r.patches.append(mock.patch.object(model_config, "RESEARCH_AUTO_APPLY", False))
        s.run()
        self.assertEqual(s.applied, [])
        self.assertIn("(off: model_config.RESEARCH_AUTO_APPLY)", s.summary())


class Lock(unittest.TestCase):
    def setUp(self):
        self.lock = Path(tempfile.mkdtemp(prefix="refresh_lock_")) / "orchestrator.lock"
        self.p = mock.patch.object(run_lock, "ORCHESTRATOR_LOCK_PATH", self.lock)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_website_button_refuses_while_another_process_holds_the_lock(self):
        import app as adapp

        self.lock.write_text(str(os.getppid()), encoding="utf-8")       # a live process that is not this one
        client = adapp.app.test_client()
        with client.session_transaction() as s:
            s["authed"] = True
        with mock.patch.object(research, "plan_refresh", side_effect=AssertionError("no edit while locked")):
            r = client.post("/research/refresh/R1")
        self.assertEqual(r.status_code, 409)
        self.assertIn("orchestrator.lock is held by PID", r.get_json()["error"])

    def test_apply_proceeds_when_the_calling_process_holds_the_lock(self):
        self.lock.write_text(str(os.getpid()), encoding="utf-8")         # the orchestrator's own lock
        with mock.patch.object(research, "plan_refresh", return_value=plan_for("R1", changes=False)):
            self.assertEqual(research.apply_refresh("R1")["applied"], False, "no lock error: it planned")


if __name__ == "__main__":
    unittest.main(verbosity=2)
