"""Pure-function tests for app/ui_web/render.py (R8.1 card seam).

DB-free: the render helpers take plain mappings, so these lock the severity
bands, the decay ceiling, the R7.3 breakdown formatting, and the gov-caution /
outreach extraction without a server or a database. The full card_view() shape
is covered end-to-end through the DOM in tests/test_ui_web_feed.py.
"""
import unittest

from app.ui_web import render


def signal(**over):
    base = {"score": None, "base_strength": None, "score_base": None,
            "score_decay": None, "score_account_fit": None,
            "score_scope_fit": None}
    base.update(over)
    return base


class TestSeverityBand(unittest.TestCase):
    def test_bands_at_boundaries(self):
        self.assertEqual(render.severity_band(None), "low")
        self.assertEqual(render.severity_band(4.0), "critical")
        self.assertEqual(render.severity_band(3.99), "high")
        self.assertEqual(render.severity_band(2.75), "high")
        self.assertEqual(render.severity_band(2.74), "moderate")
        self.assertEqual(render.severity_band(1.5), "moderate")
        self.assertEqual(render.severity_band(1.49), "low")


class TestFmtAndScope(unittest.TestCase):
    def test_fmt_score_trims_trailing_zeros(self):
        self.assertEqual(render.fmt_score(None), "unscored")
        self.assertEqual(render.fmt_score(4.0), "4")
        self.assertEqual(render.fmt_score(2.75), "2.75")
        self.assertEqual(render.fmt_score(2.50), "2.5")

    def test_scope_label_known_and_fallback(self):
        self.assertEqual(render.scope_label("regulatory_calendar"), "Regulatory")
        self.assertEqual(render.scope_label("mystery"), "mystery")


class TestDecayAndBreakdown(unittest.TestCase):
    def test_decay_ceiling_uses_fits_when_present(self):
        s = signal(base_strength=5, score_account_fit=0.8, score_scope_fit=0.5)
        self.assertEqual(render.decay_ceiling(s), 2.0)

    def test_decay_ceiling_falls_back_to_base(self):
        self.assertEqual(render.decay_ceiling(signal(base_strength=4)), 4.0)
        self.assertIsNone(render.decay_ceiling(signal(base_strength=None)))

    def test_breakdown_none_until_rescored(self):
        self.assertIsNone(render.score_breakdown(signal(score=3.0)))

    def test_breakdown_formats_components(self):
        s = signal(score=2.34, score_base=5, score_decay=0.85,
                   score_account_fit=0.55, score_scope_fit=1.0)
        self.assertEqual(render.score_breakdown(s),
                         "score 2.34 = 5 × 0.85 × 0.55 × 1.00")


class TestExtraction(unittest.TestCase):
    def test_gov_caution_line_case_insensitive(self):
        text = "Recommended path: E5\nGov-cloud caution: verify tenant"
        self.assertEqual(render.gov_caution_line(text),
                         "Gov-cloud caution: verify tenant")
        self.assertIsNone(render.gov_caution_line("no caution here"))
        self.assertIsNone(render.gov_caution_line(None))

    def test_first_outreach_skips_blank(self):
        snaps = [{"outreach_safe_text": "   "}, {"outreach_safe_text": "reach out"}]
        self.assertEqual(render.first_outreach(snaps), "reach out")
        self.assertEqual(render.first_outreach([{"outreach_safe_text": ""}]), "")


if __name__ == "__main__":
    unittest.main()
