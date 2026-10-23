"""Expected phrases: sentences a refresh inserts are stored on the ad_history entry; the verifier calls the listing
"outdated" ("new text not live") while one is missing from the live page, drops each once it is seen live, and a
regeneration or reprice clears the list. Offline: synthetic entries, a fake check function, nothing read from or
written to the real ad_history.json."""
import copy
import io
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path)
import adwriter  # noqa: E402
import verifier  # noqa: E402

TODAY = date(2026, 10, 23)
OLD = ("This 2024 GLS 450 arrives with a clean history and a full service record from new.\n\n"
       "Power comes from a turbocharged engine with a 9-speed automatic. Current asking price is $69,774 (includes $899 "
       "dealer administrative fee), $2,100 below Kelley Blue Book Typical Listing Price for comparable units.\n\n"
       "Warranty paragraph with enough words to read as an ad.\n\nCall the store today to arrange a drive.")
INSERTED = "It is rated to tow up to 7,700 lbs."
NEW = OLD.replace("9-speed automatic.", "9-speed automatic. " + INSERTED)


def entry(**kw):
    e = {"current_ad_text": NEW, "first_ad_date": "2026-10-01", "last_ad_date": "2026-10-01", "ad_count": 1,
         "verification_verdict": None, "match_score": None, "identity_confirmed": None, "price_mismatch": None,
         "last_verified": None, "verification_note": None, "last_advertised_price": 69774.0,
         "expected_phrases": [INSERTED]}
    e.update(kw)
    return e


def page(text):
    def check(stock, headless=True):
        return {"page_found": True, "url_found": "https://www.hendrickcars.com/x", "description_text": text}
    return check


def run(history, check):
    with mock.patch.object(adwriter, "save_ad_history"), \
         mock.patch.object(verifier, "load_previous_snapshot", return_value=[]), redirect_stdout(io.StringIO()):
        return verifier.run_verification(history, check=check, today=TODAY)


class Helper(unittest.TestCase):
    def test_a_refresh_records_the_sentences_it_inserted(self):
        e = {"current_ad_text": NEW}
        self.assertEqual(adwriter.note_expected_phrases(e, OLD), [INSERTED])
        self.assertEqual(e["expected_phrases"], [INSERTED])
        adwriter.note_expected_phrases(e, OLD)
        self.assertEqual(e["expected_phrases"], [INSERTED], "no duplicates")
        self.assertEqual(adwriter.note_expected_phrases({"current_ad_text": OLD}, OLD), [], "nothing new, nothing stored")

    def test_every_refresh_path_records_them(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("research.py", "tow_refresh.py", "warranty_refresh.py", "carfax_refresh.py"):
            self.assertIn("A.note_expected_phrases(e, old_text)", (root / name).read_text(encoding="utf-8"), name)


class Verifier(unittest.TestCase):
    def test_phrase_live(self):
        h = {"S1": entry()}
        run(h, page(NEW.replace("\n\n", "  \n ")))      # whitespace differs on the page
        e = h["S1"]
        self.assertEqual(e["verification_verdict"], "current")
        self.assertIsNone(e["verification_note"])
        self.assertNotIn("expected_phrases", e, "cleared once found live")

    def test_curly_quotes_and_dashes_still_match(self):
        ins = "It's rated to tow up to 7,700 lbs - with the hitch."
        h = {"S1": entry(current_ad_text=OLD.replace("automatic.", "automatic. " + ins), expected_phrases=[ins])}
        run(h, page(OLD.replace("automatic.", "automatic. It’s rated to tow up to 7,700 lbs – with the hitch.")))
        self.assertNotIn("expected_phrases", h["S1"])

    def test_phrase_missing(self):
        h = {"S1": entry()}
        _, _, upd = run(h, page(OLD))                    # the old listing: everything else matches
        e = h["S1"]
        self.assertEqual(e["verification_verdict"], "outdated")
        self.assertEqual(e["verification_note"], "new text not live")
        self.assertEqual(e["expected_phrases"], [INSERTED], "kept until it is seen live")
        self.assertEqual([r["stock_number"] for r in upd], ["S1"])

    def test_only_found_phrases_are_dropped(self):
        other = "The trailer brake controller is fitted."
        h = {"S1": entry(expected_phrases=[INSERTED, other])}
        run(h, page(NEW))
        self.assertEqual(h["S1"]["expected_phrases"], [other])
        self.assertEqual(h["S1"]["verification_note"], "new text not live")

    def test_page_not_read_keeps_the_list(self):
        h = {"S1": entry()}
        run(h, lambda stock, headless=True: {"page_found": False, "url_found": None, "description_text": None})
        self.assertEqual(h["S1"]["expected_phrases"], [INSERTED])


class Cleared(unittest.TestCase):
    def test_cleared_on_regenerate(self):
        h = {"S1": entry(stale_phrases=["old sentence that was removed"])}
        adwriter.record_ad(h, "S1", ad_text=NEW, price=68875.0, lifecycle_stage="active",
                           recon_included=True, recon_pending=False, today="2026-10-23")
        self.assertNotIn("expected_phrases", h["S1"])
        self.assertNotIn("stale_phrases", h["S1"])

    def test_cleared_on_reprice(self):
        src = (Path(__file__).resolve().parent.parent / "adwriter.py").read_text(encoding="utf-8")
        body = src[src.index("def reprice_ad("):]
        body = body[body.index('entry["lifecycle_stage"] = "repriced"'):body.index("return full")]   # the reprice's save block
        self.assertIn('entry.pop("expected_phrases", None)', body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
