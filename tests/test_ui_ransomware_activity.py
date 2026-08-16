"""Explore Ransomware Activity aggregate tests (R8.5, R4.1, R6.6).

Hermetic: real migrations against in-memory SQLite, FK on, canned payloads, no
network. Covers the aggregation itself (marginal counts, deterministic order,
derived window), the R4.1 protections that make an aggregate safe where a card
would not be (singleton crews withheld, no victim/domain/url ever returned),
the unclassified-industry bucket, and the peer flag's shared predicate with the
classifier gate. The view shaper's notes are covered alongside, since the honest
"what is NOT shown" copy is the point of the panel.
"""
import json
import sqlite3
import unittest
from datetime import datetime, timezone

from app.classify import ransomware as ransomware_classifier
from app.db.migrate import apply_migrations
from app.ui import data
from app.ui_web import render


def victim(name, group, activity, event_date="2026-08-12", domain=None):
    """One ransomware.live listing shaped like the real feed."""
    return {
        "victim": name,
        "group": group,
        "activity": activity,
        "country": "US",
        "domain": domain or (name.lower().replace(" ", "") + ".com"),
        "description": f"{name} is a company.",
        "discovered": event_date + "T00:00:00+00:00",
        "url": f"https://www.ransomware.live/id/{name}",
    }, event_date


def fixture_conn(listings=(), source_id="ransomware_live"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, enabled, ttl, "
        "access_method, evidence_rank) VALUES (?,'ransomware.live',1,3600,"
        "'json',3)", (source_id,))
    for i, (payload, event_date) in enumerate(listings):
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            "payload, url) VALUES (?,?,?,?,?)",
            (f"{source_id}:h:{i:04d}", source_id, event_date,
             json.dumps(payload), payload.get("url")))
    conn.commit()
    return conn


# A corpus shaped like the real one: a dominant crew, a mid crew, a singleton
# crew, the two energy listings, and an unclassified listing.
CORPUS = (
    victim("Alpha Mfg", "qilin", "Manufacturing"),
    victim("Beta Mfg", "qilin", "Manufacturing"),
    victim("Gamma Mfg", "qilin", "Manufacturing"),
    victim("Delta Health", "thegentlemen", "Healthcare"),
    victim("Epsilon Health", "thegentlemen", "Healthcare"),
    victim("Zeta Power", "thegentlemen", "Energy & Utilities",
           event_date="2026-08-10"),
    victim("Eta Midstream", "incransom", "Oil and Gas"),
    victim("Theta Ltd", "incransom", "Technology"),
    victim("Iota Corp", "lonewolfcrew", "Retail & E-Commerce",
           event_date="2026-08-14"),
    victim("Kappa Inc", "qilin", "Not Found"),
)


class TestRansomwareActivityCounts(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn(CORPUS)
        self.activity = data.ransomware_activity(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_total_counts_every_stored_listing(self):
        # Including the unclassified one and the withheld singleton crew's:
        # the total is the honest corpus size, not the displayed subset.
        self.assertEqual(self.activity["total"], 10)

    def test_crews_ranked_by_volume_descending(self):
        labels = [r["label"] for r in self.activity["crews"]]
        counts = [r["count"] for r in self.activity["crews"]]
        self.assertEqual(labels, ["qilin", "thegentlemen", "incransom"])
        self.assertEqual(counts, [4, 3, 2])

    def test_industries_ranked_by_volume_descending(self):
        rows = self.activity["industries"]
        self.assertEqual(rows[0]["label"], "Manufacturing")
        self.assertEqual(rows[0]["count"], 3)
        self.assertEqual([r["count"] for r in rows],
                         sorted((r["count"] for r in rows), reverse=True))

    def test_unclassified_is_its_own_bucket_not_an_industry_row(self):
        self.assertEqual(self.activity["unclassified"], 1)
        self.assertNotIn("Not Found",
                         [r["label"] for r in self.activity["industries"]])

    def test_window_is_derived_from_the_data_inclusive(self):
        self.assertEqual(self.activity["window_start"], "2026-08-10")
        self.assertEqual(self.activity["window_end"], "2026-08-14")
        # 10th..14th inclusive is 5 days, not 4 — the panel states a real span.
        self.assertEqual(self.activity["window_days"], 5)


class TestPeerFlagSharesTheClassifierGate(unittest.TestCase):
    """The highlighted rows must be EXACTLY the rows that mint peer cards."""

    def test_energy_and_oil_and_gas_flagged_others_not(self):
        conn = fixture_conn(CORPUS)
        rows = {r["label"]: r["is_peer"]
                for r in data.ransomware_activity(conn)["industries"]}
        conn.close()
        self.assertTrue(rows["Energy & Utilities"])
        self.assertTrue(rows["Oil and Gas"])
        self.assertFalse(rows["Manufacturing"])
        self.assertFalse(rows["Healthcare"])
        self.assertFalse(rows["Technology"])

    def test_peer_listings_totals_the_flagged_rows(self):
        conn = fixture_conn(CORPUS)
        activity = data.ransomware_activity(conn)
        conn.close()
        self.assertEqual(activity["peer_listings"], 2)

    def test_flag_matches_the_classifier_predicate_for_every_row(self):
        # If the gate widens or narrows, this panel follows it automatically
        # rather than drifting into a claim the classifier no longer makes.
        conn = fixture_conn(CORPUS)
        activity = data.ransomware_activity(conn)
        conn.close()
        for row in activity["industries"]:
            self.assertEqual(
                row["is_peer"],
                ransomware_classifier.is_peer_industry(row["label"]),
                f"panel and classifier disagree on {row['label']!r}")


class TestAggregatePrivacy(unittest.TestCase):
    """R4.1 in aggregate: an N:1 count is safe only while N > 1."""

    def setUp(self):
        self.conn = fixture_conn(CORPUS)
        self.activity = data.ransomware_activity(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_singleton_crew_name_is_never_returned(self):
        # A crew with one listing maps 1:1 back to one company, and a crew that
        # named itself after its victim would leak that company's identity.
        self.assertNotIn("lonewolfcrew",
                         [r["label"] for r in self.activity["crews"]])

    def test_withheld_crews_are_still_counted_and_reported(self):
        self.assertEqual(self.activity["crews_withheld"], 1)
        self.assertEqual(self.activity["crews_withheld_listings"], 1)
        # crew_total counts every distinct crew, named or not, so the panel can
        # say how much it is holding back rather than implying completeness.
        self.assertEqual(self.activity["crew_total"], 4)

    def test_crew_requires_multiple_distinct_victims_to_be_named(self):
        # The ingest path can store more than one raw listing for one real
        # victim when the upstream record changes. Two listings for a self-named
        # crew still map back to one company, so raw listing count is not enough
        # to clear the privacy gate.
        conn = fixture_conn((
            victim("Alpha Mfg", "Alpha Mfg", "Manufacturing",
                   event_date="2026-08-10", domain="alpha.example"),
            victim("Alpha Mfg", "Alpha Mfg", "Manufacturing",
                   event_date="2026-08-11", domain="alpha.example"),
        ))
        activity = data.ransomware_activity(conn)
        conn.close()
        self.assertEqual(activity["total"], 2)
        self.assertEqual(activity["crews"], [])
        self.assertEqual(activity["crews_withheld"], 1)
        self.assertEqual(activity["crews_withheld_listings"], 2)

    def test_no_victim_domain_or_url_anywhere_in_the_result(self):
        blob = json.dumps(self.activity).lower()
        for leak in ("alpha mfg", "zeta power", "alphamfg.com", "zetapower.com",
                     "ransomware.live/id", "is a company"):
            self.assertNotIn(leak, blob, f"{leak!r} reached the view layer")

    def test_result_carries_no_score_entity_or_signal_identity(self):
        # This is a counts surface, not a card surface.
        for forbidden in ("score", "entity_id", "signal_id", "confidence"):
            self.assertNotIn(forbidden, self.activity)


class TestRansomwareActivityEdges(unittest.TestCase):
    def test_empty_store_yields_zeroed_counts_not_an_error(self):
        conn = fixture_conn(())
        activity = data.ransomware_activity(conn)
        conn.close()
        self.assertEqual(activity["total"], 0)
        self.assertEqual(activity["crews"], [])
        self.assertEqual(activity["industries"], [])
        self.assertEqual(activity["window_days"], 0)
        self.assertEqual(activity["window_start"], "")

    def test_other_sources_are_not_counted(self):
        conn = fixture_conn(CORPUS, source_id="some_other_feed")
        activity = data.ransomware_activity(conn)
        conn.close()
        self.assertEqual(activity["total"], 0)

    def test_malformed_and_empty_payloads_do_not_crash(self):
        conn = fixture_conn(())
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            "payload, url) VALUES ('r1','ransomware_live','2026-08-12',"
            "'not json','http://x')")
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            "payload, url) VALUES ('r2','ransomware_live','2026-08-12',"
            "'[1,2,3]','http://x')")
        conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            "payload, url) VALUES ('r3','ransomware_live','2026-08-12',"
            "NULL,'http://x')")
        activity = data.ransomware_activity(conn)
        conn.close()
        self.assertEqual(activity["total"], 3)
        self.assertEqual(activity["crews"], [])
        # No usable industry on any of them -> all unclassified, none dropped.
        self.assertEqual(activity["unclassified"], 3)

    def test_blank_and_unknown_activity_read_as_unclassified(self):
        conn = fixture_conn((
            victim("A Co", "crewx", ""),
            victim("B Co", "crewx", "unknown"),
            victim("C Co", "crewx", "N/A"),
        ))
        activity = data.ransomware_activity(conn)
        conn.close()
        self.assertEqual(activity["unclassified"], 3)
        self.assertEqual(activity["industries"], [])


class TestSourceAttributionAndFreshness(unittest.TestCase):
    """R10.4 credit is a licence condition; R6.6 stale must not read as quiet."""

    NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

    def _with_run(self, finished_at, status="success"):
        conn = fixture_conn(CORPUS)
        conn.execute(
            "INSERT INTO source_runs (run_id, source_id, started_at, "
            "finished_at, status, error_state) VALUES "
            "('r1','ransomware_live',?,?,?,'')",
            (finished_at, finished_at, status))
        conn.commit()
        return conn

    def test_source_is_named_and_linked_on_the_populated_panel(self):
        conn = self._with_run("2026-08-14T11:00:00+00:00")
        view = render.ransomware_activity_view(
            data.ransomware_activity(conn, now=self.NOW))
        conn.close()
        # CC-BY-4.0 requires credit wherever the data is presented, so naming
        # the source only in the empty state is not enough.
        self.assertIn("ransomware.live", view["source_name"])
        self.assertEqual(view["source_url"], "https://www.ransomware.live")
        self.assertIn("Source:", view["source_line"])

    def test_fresh_run_states_the_ingest_time_and_is_not_stale(self):
        conn = self._with_run("2026-08-14T11:00:00+00:00")
        view = render.ransomware_activity_view(
            data.ransomware_activity(conn, now=self.NOW))
        conn.close()
        self.assertFalse(view["stale"])
        self.assertIn("last ingested", view["source_line"])
        self.assertIn("2026-08-14T11:00:00+00:00", view["source_line"])

    def test_stalled_feed_is_flagged_rather_than_reading_as_a_quiet_week(self):
        # ttl is 3600s in the fixture; a run 5 days old is well past it. The
        # derived window is unchanged, which is exactly the trap.
        conn = self._with_run("2026-08-09T11:00:00+00:00")
        activity = data.ransomware_activity(conn, now=self.NOW)
        view = render.ransomware_activity_view(activity)
        conn.close()
        self.assertEqual(activity["source_state"], "stale")
        self.assertTrue(view["stale"])
        self.assertIn("STALE", view["source_line"])
        # The counts themselves are untouched — only the framing changes.
        self.assertEqual(view["total"], 10)

    def test_never_ingested_says_so_instead_of_showing_a_bare_timestamp(self):
        conn = fixture_conn(CORPUS)
        view = render.ransomware_activity_view(
            data.ransomware_activity(conn, now=self.NOW))
        conn.close()
        self.assertIn("never ingested", view["source_line"])


class TestUnclassifiedIsCharted(unittest.TestCase):
    """The largest bucket must not be footnote-only (it out-counts the top
    named industry on the real corpus)."""

    def test_unclassified_gets_its_own_row_ranked_by_count(self):
        # 3 unclassified vs 2 Manufacturing: unclassified must sort FIRST, so a
        # skimmer cannot read Manufacturing as the most-targeted industry.
        conn = fixture_conn((
            victim("A Co", "crewx", "Not Found"),
            victim("B Co", "crewx", "Not Found"),
            victim("C Co", "crewx", ""),
            victim("D Co", "crewy", "Manufacturing"),
            victim("E Co", "crewy", "Manufacturing"),
        ))
        view = render.ransomware_activity_view(data.ransomware_activity(conn))
        conn.close()
        rows = view["industries"]
        self.assertEqual(rows[0]["label"], "No industry given")
        self.assertEqual(rows[0]["count"], 3)
        self.assertTrue(rows[0]["unclassified"])
        # It is an absence of evidence, never a peer match.
        self.assertFalse(rows[0]["is_peer"])

    def test_no_unclassified_row_when_every_listing_is_classified(self):
        conn = fixture_conn((victim("A Co", "crewx", "Manufacturing"),))
        view = render.ransomware_activity_view(data.ransomware_activity(conn))
        conn.close()
        self.assertEqual([r["label"] for r in view["industries"]],
                         ["Manufacturing"])


class TestPeerCardSourceUrlIsIdentitySafe(unittest.TestCase):
    """R4.1: ransomware.live's /id/ path is base64("<Victim>@<crew>"), so a
    name-free sector card that links its own permalink names the company it
    just refused to name. The Explore panel states the peer set's exact size,
    which turns that link into a certain re-identification."""

    VICTIM_URL = ("https://www.ransomware.live/id/"
                  "Q29tbXVuaXR5IENvbm5lY3Rpb25zQHRoZWdlbnRsZW1lbg==")

    def test_sector_card_does_not_link_the_per_victim_permalink(self):
        safe = data.identity_safe_source_url(self.VICTIM_URL, "sector")
        self.assertEqual(safe, "https://www.ransomware.live")
        # The decodable victim segment is gone entirely, not merely hidden.
        self.assertNotIn("Q29tbXVuaXR5", safe)

    def test_attribution_survives_the_strip(self):
        # R10.4 credit must not be collateral damage: still a ransomware.live
        # link, just not a per-victim one.
        safe = data.identity_safe_source_url(self.VICTIM_URL, "subsector")
        self.assertIn("ransomware.live", safe)

    def test_account_card_keeps_its_permalink(self):
        # An own_incident card already names its entity, so the permalink
        # discloses nothing the card does not.
        for scope in ("account", "parent"):
            self.assertEqual(
                data.identity_safe_source_url(self.VICTIM_URL, scope),
                self.VICTIM_URL)

    def test_non_ransomware_urls_are_untouched(self):
        other = "https://www.sec.gov/Archives/edgar/data/1234/0001.htm"
        self.assertEqual(data.identity_safe_source_url(other, "sector"), other)

    def test_empty_url_stays_empty(self):
        self.assertEqual(data.identity_safe_source_url("", "sector"), "")
        self.assertEqual(data.identity_safe_source_url(None, "sector"), "")


class TestLeakTrackerHostRule(unittest.TestCase):
    """The gate is a HOST rule, not a substring one (R4.1, defence in depth).

    Matching "ransomware.live/id/" anywhere in the lowercased URL happened to
    get the shapes ingest emits right, and got everything else wrong: a port
    breaks the substring, a non-/id/ victim path has none of it, any other
    leak-site host has none of it, and a URL that merely MENTIONS the tracker in
    a query string matched. The ingest path only emits the bare /id/ form today,
    so none of these variants is reachable in production — this table is the
    demonstration that the rule holds for the ones that are not.
    """

    VICTIM = "Q29tbXVuaXR5IENvbm5lY3Rpb25zQHRoZWdlbnRsZW1lbg=="

    def test_tracker_hosts_are_substituted_however_spelled(self):
        for url in (
                f"https://ransomware.live/id/{self.VICTIM}",
                f"https://www.ransomware.live/id/{self.VICTIM}",
                f"https://RANSOMWARE.LIVE/id/{self.VICTIM}",
                f"https://WWW.Ransomware.Live/id/{self.VICTIM}",
                f"https://ransomware.live:8443/id/{self.VICTIM}",
                f"http://ransomware.live/id/{self.VICTIM}",
                # a victim path the tracker does not use today
                "https://www.ransomware.live/victims/Acme%20Corp",
        ):
            with self.subTest(url=url):
                self.assertEqual(data.identity_safe_source_url(url, "sector"),
                                 data.RANSOMWARE_SOURCE_URL)

    def test_lookalike_hosts_are_not_substituted(self):
        # a substring rule matched both of these; a host rule must not.
        for url in (
                "https://evil.com/?x=ransomware.live/id/abc",
                f"https://ransomware.live.evil.com/id/{self.VICTIM}",
        ):
            with self.subTest(url=url):
                self.assertEqual(data.identity_safe_source_url(url, "sector"),
                                 url)

    def test_account_scope_passes_the_tracker_url_through_untouched(self):
        url = f"https://www.ransomware.live/id/{self.VICTIM}"
        for scope in ("account", "parent"):
            with self.subTest(scope=scope):
                self.assertEqual(data.identity_safe_source_url(url, scope), url)


class TestRansomwareActivityView(unittest.TestCase):
    def setUp(self):
        conn = fixture_conn(CORPUS)
        self.view = render.ransomware_activity_view(
            data.ransomware_activity(conn))
        conn.close()

    def test_window_label_states_the_real_span(self):
        self.assertEqual(self.view["window"], "2026-08-10 → 2026-08-14 · 5 days")

    def test_crew_note_explains_what_is_withheld_and_why(self):
        note = self.view["crew_note"]
        self.assertIn("1 further crew is not named", note)
        # The note must state the gate as it actually is — a distinct-VICTIM
        # floor, not a listing floor (a revised record makes a second listing
        # for the same company, and that crew is still withheld).
        self.assertIn("tied to a single victim", note)
        self.assertIn("still counted in the total", note)
        self.assertNotIn("lonewolfcrew", note)

    def test_crew_note_counts_listings_for_a_multi_listing_withheld_crew(self):
        # One victim, two listings: the note says one victim, two listings.
        conn = fixture_conn((
            victim("Alpha Mfg", "Alpha Mfg", "Manufacturing",
                   event_date="2026-08-10", domain="alpha.example"),
            victim("Alpha Mfg", "Alpha Mfg", "Manufacturing",
                   event_date="2026-08-11", domain="alpha.example"),
        ))
        note = render.ransomware_activity_view(
            data.ransomware_activity(conn))["crew_note"]
        conn.close()
        self.assertIn("tied to a single victim", note)
        self.assertIn("2 listings in total", note)
        self.assertNotIn("Alpha Mfg", note)

    def test_industry_note_states_the_unclassified_share(self):
        self.assertIn("1 of 10", self.view["industry_note"])
        self.assertIn("unclassified", self.view["industry_note"])

    def test_peer_note_states_peers_against_the_total(self):
        self.assertIn("2 of 10", self.view["peer_note"])

    def test_caption_disclaims_score_and_account_implication(self):
        caption = self.view["caption"].lower()
        self.assertIn("unverified", caption)
        self.assertIn("nothing on this tab is scored", caption)
        self.assertIn("attributed to a watchlist account", caption)
        self.assertIn("watchlist-industry rows", caption)

    def test_bars_are_relative_within_their_own_list(self):
        # Top row is full width; every row gets a visible minimum.
        self.assertEqual(self.view["crews"][0]["bar"], 100)
        self.assertTrue(all(r["bar"] >= 2 for r in self.view["crews"]))
        self.assertEqual(self.view["industries"][0]["bar"], 100)

    def test_empty_activity_flags_empty_without_notes(self):
        conn = fixture_conn(())
        view = render.ransomware_activity_view(data.ransomware_activity(conn))
        conn.close()
        self.assertTrue(view["empty"])
        self.assertEqual(view["window"], "")
        self.assertEqual(view["crew_note"], "")

    def test_single_day_corpus_reads_one_day_not_zero(self):
        conn = fixture_conn((victim("Solo Co", "crewx", "Healthcare"),
                             victim("Duo Co", "crewx", "Healthcare")))
        view = render.ransomware_activity_view(data.ransomware_activity(conn))
        conn.close()
        self.assertIn("1 day", view["window"])
        self.assertNotIn("1 days", view["window"])


if __name__ == "__main__":
    unittest.main()
