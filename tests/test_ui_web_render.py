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


class TestSafeAttributes(unittest.TestCase):
    def test_feedback_dom_id_is_stable_and_selector_safe(self):
        raw = "t:presswire:https://example.com/a?x=1&y=2:E"
        dom_id = render.feedback_dom_id(raw)
        self.assertRegex(dom_id, r"^gs-fb-[0-9a-f]{16}$")
        self.assertEqual(dom_id, render.feedback_dom_id(raw))

    def test_safe_source_url_allows_only_http_s(self):
        self.assertEqual(render.safe_source_url("https://example.com/doc"),
                         "https://example.com/doc")
        self.assertEqual(render.safe_source_url(" http://example.com/doc "),
                         "http://example.com/doc")
        self.assertIsNone(render.safe_source_url("javascript:alert(1)"))
        self.assertIsNone(render.safe_source_url("data:text/html,hi"))
        self.assertIsNone(render.safe_source_url(""))


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


class TestAccountHeaderView(unittest.TestCase):
    def _entity(self, **over):
        base = {"entity_id": "E1", "name": "Acme", "subsector": "iou_electric",
                "richness": "high", "coverage_flag": "edgar-visible",
                "gov_cloud_likelihood": "possible",
                "tenant_cloud_environment": "commercial",
                "cik": "0000123", "lei": None, "wikidata_qid": None,
                "ticker": "ACME"}
        base.update(over)
        return base

    def _view(self, entity=None, parent=None, children=None):
        return render.account_header_view(
            {"entity": entity or self._entity(), "parent": parent,
             "children": children or []})

    def test_identifiers_skip_blank_and_append_entity_id(self):
        v = self._view()
        self.assertEqual(v["identifiers"],
                         ["CIK 0000123", "Ticker ACME", "entity_id E1"])

    def test_dark_coverage_reads_low_coverage(self):
        v = self._view(self._entity(coverage_flag="dark"))
        self.assertEqual(v["coverage_badge"]["cls"], "gs-badge coverage-dark")
        self.assertIn("low coverage", v["coverage_badge"]["text"])

    def test_non_dark_coverage_labeled(self):
        self.assertEqual(self._view()["coverage_badge"]["text"],
                         "coverage: edgar-visible")

    def test_missing_columns_default_unknown_and_name_falls_back(self):
        # a sparse row missing posture columns still shapes without raising
        v = self._view({"entity_id": "E2", "name": None})
        self.assertEqual(v["name"], "E2")           # falls back to entity_id
        self.assertEqual(v["subsector"], "unknown")
        self.assertEqual(v["gov_cloud"], "unknown")
        self.assertEqual(v["identifiers"], ["entity_id E2"])

    def test_parent_and_children_carry_entity_id_for_links(self):
        v = self._view(parent={"entity_id": "E0", "name": "Parent Co"},
                       children=[{"entity_id": "E9", "name": "Sub Co"}])
        self.assertEqual(v["parent"], {"entity_id": "E0", "name": "Parent Co"})
        self.assertEqual(v["children"][0]["entity_id"], "E9")


class TestTimelineRows(unittest.TestCase):
    def test_rows_shape_and_scope_label(self):
        rows = render.timeline_rows([
            {"event_date": "2026-07-27", "headline": "Acme names CISO",
             "signal_scope": "account"},
            {"event_date": None, "headline": None,
             "signal_scope": "regulatory_calendar"}])
        self.assertEqual(rows[0], {"date": "2026-07-27",
                                   "headline": "Acme names CISO",
                                   "scope_label": "Account"})
        self.assertEqual(rows[1], {"date": "", "headline": "",
                                   "scope_label": "Regulatory"})


if __name__ == "__main__":
    unittest.main()
