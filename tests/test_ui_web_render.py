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


class TestExploreAnalyticsView(unittest.TestCase):
    """U9: analytics counts -> table view dicts, each row keeping its dimension
    identity so a count is inspectable back to its sourced signals (R4.1)."""

    def test_tables_built_per_nonempty_dimension(self):
        counts = {
            "trigger": [{"key": "t_lead", "label": "Leadership", "count": 4}],
            "scope": [{"key": "account", "label": "account", "count": 4}],
            "incident_tier": [],
        }
        tables = render.explore_analytics_view(counts)
        dims = [t["dimension"] for t in tables]
        self.assertEqual(dims, ["trigger", "scope"])   # empty tier dropped
        trig = tables[0]
        self.assertEqual(trig["label"], "Trigger")
        self.assertEqual(trig["rows"][0],
                         {"key": "t_lead", "label": "Leadership", "count": 4})

    def test_empty_counts_yield_empty_list(self):
        self.assertEqual(
            render.explore_analytics_view(
                {"trigger": [], "scope": [], "incident_tier": []}),
            [])


class TestExploreMapDensity(unittest.TestCase):
    """U9: the choropleth maps counts to a FIXED absolute scale (D5)."""

    def test_fixed_scale_buckets(self):
        self.assertEqual(render.map_density_class(0), "gs-map-d0")
        self.assertEqual(render.map_density_class(1), "gs-map-d1")
        self.assertEqual(render.map_density_class(2), "gs-map-d1")
        self.assertEqual(render.map_density_class(3), "gs-map-d2")
        self.assertEqual(render.map_density_class(7), "gs-map-d3")
        self.assertEqual(render.map_density_class(15), "gs-map-d4")
        self.assertEqual(render.map_density_class(999), "gs-map-d4")


class TestExploreMapSvg(unittest.TestCase):
    """U9: the inline-SVG map. Gated points -> one <circle> each; empty input ->
    base geography + honest note (R6.6); a known TX facility projects INSIDE
    Texas's path bounding box (projection regression, KTD5)."""

    def _facility(self, **over):
        base = {"facility_id": "F1", "facility_name": "Plant",
                "latitude": 30.3, "longitude": -97.7, "capacity_mw": 500,
                "facility_owner_confidence": 0.9, "entity_id": "E_ACME",
                "entity_name": "Acme Energy", "subsector": "iou_electric"}
        base.update(over)
        return base

    def _state_row(self, **over):
        base = {"facility_id": "F1", "latitude": 30.3, "longitude": -97.7,
                "confidence": 0.9, "entity_id": "E_ACME",
                "entity_name": "Acme Energy", "subsector": "iou_electric",
                "signal_count": 4}
        base.update(over)
        return base

    def test_one_circle_per_gated_point(self):
        view = render.explore_map_svg([self._facility(), self._facility(
            facility_id="F2", facility_name="Plant 2")], [])
        self.assertEqual(view["svg"].count("<circle"), 2)
        self.assertTrue(view["has_facilities"])
        self.assertIsNone(view["empty_note"])

    def test_every_state_and_facility_emits_a_title(self):
        view = render.explore_map_svg([self._facility()], [self._state_row()])
        # 49 state paths + 1 facility -> 50 <title> elements.
        self.assertEqual(view["svg"].count("<title>"), 50)
        self.assertEqual(view["svg"].count("<path"), 49)
        self.assertIn("Texas:", view["svg"])            # a state title
        self.assertIn("Acme Energy", view["svg"])        # the facility title

    def test_empty_input_renders_base_map_and_honest_note(self):
        view = render.explore_map_svg([], [])
        self.assertEqual(view["svg"].count("<circle"), 0)
        self.assertEqual(view["svg"].count("<path"), 49)   # base geography
        self.assertFalse(view["has_facilities"])
        self.assertIn("No facility-level evidence yet", view["empty_note"])

    def test_offmap_facility_omitted_not_misplaced(self):
        # An Alaska coordinate is outside the continental bounds -> no circle,
        # and it must not be attributed to any state's density.
        ak = self._facility(facility_id="F_AK", latitude=61.2, longitude=-149.9)
        view = render.explore_map_svg([ak], [])
        self.assertEqual(view["svg"].count("<circle"), 0)
        self.assertFalse(view["has_facilities"])

    def test_density_shades_state_from_projected_facility(self):
        # A TX facility whose owner has 4 signals -> Texas gets the 3-6 bucket.
        view = render.explore_map_svg(
            [self._facility()], [self._state_row(signal_count=4)])
        # The TX path must carry the d2 (3-6) density class.
        import re
        tx = re.search(r'<path class="gs-map-state ([a-z0-9-]+)" '
                       r'data-state="TX"', view["svg"])
        self.assertIsNotNone(tx)
        self.assertEqual(tx.group(1), "gs-map-d2")

    def test_density_counts_owner_once_per_state(self):
        # Two TX facilities for the same owner still represent 4 owner signals,
        # not 8 facility-multiplied signals.
        view = render.explore_map_svg(
            [self._facility(), self._facility(facility_id="F2")],
            [self._state_row(facility_id="F1", signal_count=4),
             self._state_row(facility_id="F2", signal_count=4)])
        self.assertIn("Texas: 4 signals", view["svg"])
        self.assertNotIn("Texas: 8 signals", view["svg"])

    def test_tx_projection_lands_inside_texas_bbox(self):
        # Projection regression (KTD5): the exact formula must place Austin, TX
        # inside the TX path's bounding box in the baked geometry.
        from app.ui_web import us_geometry
        x, y = us_geometry.project(-97.7, 30.3)
        tx_d = next(d for usps, _n, d in us_geometry.STATE_PATHS if usps == "TX")
        x0, y0, x1, y1 = render._bbox_of_path(tx_d)
        self.assertTrue(x0 <= x <= x1, f"x {x} not in [{x0},{x1}]")
        self.assertTrue(y0 <= y <= y1, f"y {y} not in [{y0},{y1}]")
        # And the point is attributed to TX by the containment helper.
        self.assertEqual(render._state_for_point(x, y), "TX")

    def test_title_text_is_escaped(self):
        # An entity name with markup must be escaped inside the <title>.
        view = render.explore_map_svg(
            [self._facility(entity_name="<script>x</script>")], [])
        self.assertNotIn("<script>x</script>", view["svg"])
        self.assertIn("&lt;script&gt;", view["svg"])


if __name__ == "__main__":
    unittest.main()
