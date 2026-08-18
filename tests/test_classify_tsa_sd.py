"""app/classify/tsa_sd.py unit tests (R4.1, R8, U7 of the combo-engine
plan).

is_pipeline_lng_relevant is a pure function with no store/network
dependency - these are direct unit tests of the gate in isolation.
tests/test_classify_regulatory.py covers it wired into
classify_federal_register end to end.
"""
import unittest

from app.classify.tsa_sd import PIPELINE_LNG_TERMS, is_pipeline_lng_relevant


class IsPipelineLngRelevantTest(unittest.TestCase):
    def test_true_for_each_pipeline_lng_term(self):
        for term in PIPELINE_LNG_TERMS:
            with self.subTest(term=term):
                self.assertTrue(is_pipeline_lng_relevant(
                    f"tsa security directive for {term} owner/operators"))

    def test_false_for_rail_and_aviation_text(self):
        self.assertFalse(is_pipeline_lng_relevant(
            "tsa security directive for rail owner/operators"))
        self.assertFalse(is_pipeline_lng_relevant(
            "air cargo screening program fee adjustment"))

    def test_false_for_empty_text(self):
        self.assertFalse(is_pipeline_lng_relevant(""))


if __name__ == "__main__":
    unittest.main()
