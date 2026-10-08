"""The CARFAX SENTENCE is a required sentence: when the model rewords it (and the
reasoning filter strips the rewrite, T23151A on 2026-10-07), the verbatim
sentence goes back in right after the provenance sentence. Offline."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adwriter as A  # noqa: E402

PROV = "One owner, local trade-in, personal use confirmed by Carfax."
CF = "Clean vehicle history."
P1 = ("This is a Hendrick Certified 2021 Honda Accord Hybrid EX-L sedan with 51,909 miles. " + PROV +
      " This vehicle is currently undergoing Hendrick Certified reconditioning and inspection prior to delivery.")
AD = P1 + "\n\nParagraph two.\n\nParagraph three.\n\nParagraph four."


class CarfaxRequired(unittest.TestCase):
    def test_key_is_required(self):
        self.assertIn("carfax_sentence", A.REQUIRED_SENTENCE_KEYS)
        self.assertEqual(A.required_sentences_from({"carfax_sentence": CF})["carfax_sentence"], CF)

    def test_reworded_sentence_is_missing_then_inserted_after_provenance(self):
        req = {"provenance_sentence": PROV, "carfax_sentence": CF}
        missing = A.missing_required_sentences(AD, req)
        self.assertEqual(missing, ["carfax_sentence"])
        out = A.insert_required_sentences(AD, req, missing, status_code=11, stock="T23151A")
        p1 = A.split_ad_paragraphs(out)["paragraph_one"]
        self.assertIn(PROV + " " + CF + " This vehicle is currently undergoing", p1)
        self.assertEqual(A.missing_required_sentences(out, req), [])
        self.assertEqual(A.split_ad_paragraphs(out)["paragraph_two"], "Paragraph two.")

    def test_multi_sentence_carfax_sentence(self):
        cf2 = "Clean vehicle history. Averaging 6,186 miles per year against the national average of roughly 15,000."
        req = {"provenance_sentence": PROV, "carfax_sentence": cf2}
        out = A.insert_required_sentences(AD, req, ["carfax_sentence"], status_code=10)
        self.assertIn(PROV + " " + cf2, out)

    def test_models_split_copy_is_replaced_not_duplicated(self):
        # V23385A, 10/8: the model wrote "Clean vehicle history. Averaging ..." for the
        # required "Clean vehicle history, averaging ...": insert, don't duplicate.
        cf = "Clean vehicle history, averaging 8,352 miles per year against the national average of roughly 15,000."
        p1 = ("Hendrick Affordable 2016 Mercedes-Benz GLE 350 with 83,352 miles. " + PROV +
              " Clean vehicle history. Averaging 8,352 miles per year against the national average of roughly 15,000."
              " This vehicle is currently undergoing pre-sale inspection.")
        ad = "\n\n".join([p1, "Paragraph two.", "Paragraph three.", "Paragraph four."])
        req = {"provenance_sentence": PROV, "carfax_sentence": cf}
        out = A.insert_required_sentences(ad, req, A.missing_required_sentences(ad, req), status_code=12)
        got = A.split_ad_paragraphs(out)["paragraph_one"]
        self.assertEqual(got.count("Clean vehicle history"), 1)
        self.assertEqual(got.count("miles per year"), 1)
        self.assertIn(PROV + " " + cf + " This vehicle is currently undergoing", got)


if __name__ == "__main__":
    unittest.main()
