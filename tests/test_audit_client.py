"""Hermetic tests for the audit judge client + schema (R9.8, R9.9, R9.12).

No network: every test injects a fake transport that returns a canned Anthropic
response. Covers verdict parsing, cost/budget accounting, and the typed
degradation results (unavailable / over_budget / error) that keep the pipeline
from crashing.
"""
import json
import unittest

from app.audit import config as cfg
from app.audit import schema
from app.audit.client import AuditClient

SIGNAL_RECORD = {
    "signal_id": "leadership_change:src:e1",
    "headline": "Acme Power names new CISO",
    "entity_name": "Acme Power",
    "signal_scope": "account",
    "trigger_id": "leadership_change",
    "trigger_name": "Leadership change",
    "evidence_quality": "primary",
    "incident_evidence_level": None,
    "raw_source_text": "Acme Power Corp today announced a new CISO ...",
    "evidence": [{"text": "announced a new CISO", "locator": "para 1"}],
    "license_plays": [],
}

VALID_VERDICT = {
    "entity_match": {"result": "pass", "notes": "Acme Power is the subject."},
    "classification": {"result": "pass", "notes": "Leadership change fits."},
    "evidence_support": {"result": "pass", "notes": "Snippet is verbatim."},
    "license_play_support": {"result": "not_applicable", "notes": "No play."},
}


def canned_transport(verdict=VALID_VERDICT, status=200, in_tok=1200,
                     out_tok=120, body_text=None):
    """Return a transport callable that yields one canned response and records
    the request bytes it was handed (so tests can assert request shape)."""
    calls = []

    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body,
                      "timeout": timeout})
        text = body_text if body_text is not None else json.dumps(verdict)
        payload = {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        }
        if status != 200:
            payload = {"error": {"type": "invalid_request_error",
                                 "message": "bad request"}}
        return status, json.dumps(payload).encode("utf-8")

    transport.calls = calls
    return transport


class TestSchema(unittest.TestCase):
    def test_versions_are_set(self):
        self.assertTrue(schema.PROMPT_VERSION)
        self.assertTrue(schema.PARSER_VERSION)

    def test_request_body_uses_structured_output_and_system(self):
        body = schema.build_request_body(SIGNAL_RECORD, "claude-haiku-4-5", 1024)
        self.assertEqual(body["model"], "claude-haiku-4-5")
        self.assertEqual(body["system"], schema.SYSTEM_PROMPT)
        fmt = body["output_config"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        # objective-only: schema forbids extra fields (no usefulness score)
        self.assertFalse(fmt["schema"]["additionalProperties"])
        self.assertEqual(set(fmt["schema"]["required"]), set(schema.CHECKS))

    def test_judge_input_includes_source_and_evidence(self):
        text = schema.build_judge_input(SIGNAL_RECORD)
        self.assertIn("Raw source text", text)
        self.assertIn("announced a new CISO", text)
        self.assertIn("Acme Power", text)

    def test_parse_response_valid(self):
        resp = {"content": [{"type": "text", "text": json.dumps(VALID_VERDICT)}]}
        out = schema.parse_response(resp)
        self.assertEqual(out["entity_match"]["result"], "pass")
        self.assertEqual(out["license_play_support"]["result"], "not_applicable")

    def test_parse_response_rejects_missing_check(self):
        partial = dict(VALID_VERDICT)
        partial.pop("classification")
        resp = {"content": [{"type": "text", "text": json.dumps(partial)}]}
        with self.assertRaises(ValueError):
            schema.parse_response(resp)

    def test_parse_response_rejects_bad_result(self):
        bad = json.loads(json.dumps(VALID_VERDICT))
        bad["entity_match"]["result"] = "useful"  # not a valid verdict value
        resp = {"content": [{"type": "text", "text": json.dumps(bad)}]}
        with self.assertRaises(ValueError):
            schema.parse_response(resp)

    def test_parse_response_rejects_non_json_text(self):
        resp = {"content": [{"type": "text", "text": "not json"}]}
        with self.assertRaises(ValueError):
            schema.parse_response(resp)


class TestClient(unittest.TestCase):
    def test_ok_verdict_and_cost_accounting(self):
        t = canned_transport(in_tok=1_000_000, out_tok=0)
        client = AuditClient(api_key="sk-test", transport=t,
                             model_id="claude-haiku-4-5")
        res = client.judge(SIGNAL_RECORD)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.verdicts["entity_match"]["result"], "pass")
        # 1M input tokens at $1/1M = $1.00
        self.assertAlmostEqual(res.cost_usd, 1.0, places=6)
        self.assertAlmostEqual(client.spent_usd, 1.0, places=6)
        self.assertEqual(client.calls, 1)

    def test_unavailable_without_key(self):
        client = AuditClient(api_key="", transport=canned_transport())
        res = client.judge(SIGNAL_RECORD)
        self.assertEqual(res.status, "unavailable")
        self.assertIsNone(res.verdicts)
        # no call was made
        self.assertEqual(client.calls, 0)

    def test_over_budget_stops_calls(self):
        t = canned_transport()
        client = AuditClient(api_key="sk-test", transport=t, max_usd=0.0)
        res = client.judge(SIGNAL_RECORD)
        self.assertEqual(res.status, "over_budget")
        self.assertEqual(len(t.calls), 0)

    def test_budget_ceiling_reached_after_spend(self):
        # tiny cap: first call spends, second is refused as over_budget
        t = canned_transport(in_tok=1_000_000, out_tok=0)  # $1 per call
        client = AuditClient(api_key="sk-test", transport=t, max_usd=0.5)
        first = client.judge(SIGNAL_RECORD)
        self.assertEqual(first.status, "ok")
        second = client.judge(SIGNAL_RECORD)
        self.assertEqual(second.status, "over_budget")

    def test_http_error_is_contained(self):
        t = canned_transport(status=400)
        client = AuditClient(api_key="sk-test", transport=t)
        res = client.judge(SIGNAL_RECORD)
        self.assertEqual(res.status, "error")
        self.assertIn("HTTP 400", res.error)

    def test_unparseable_verdict_is_error_but_still_charged(self):
        t = canned_transport(body_text="{not valid json", in_tok=100, out_tok=10)
        client = AuditClient(api_key="sk-test", transport=t)
        res = client.judge(SIGNAL_RECORD)
        self.assertEqual(res.status, "error")
        self.assertIn("parse error", res.error)
        # a paid-for call counts against the budget even if output was junk
        self.assertGreater(client.spent_usd, 0)

    def test_transport_exception_is_contained(self):
        def boom(url, headers, body, timeout):
            raise TimeoutError("connection timed out")
        client = AuditClient(api_key="sk-secret-should-not-leak", transport=boom)
        res = client.judge(SIGNAL_RECORD)
        self.assertEqual(res.status, "error")
        self.assertIn("transport", res.error)
        # the key must never appear in a surfaced error string
        self.assertNotIn("sk-secret-should-not-leak", res.error)

    def test_default_transport_is_urllib(self):
        # constructing without a transport must not require network; only judge()
        # would call out. Just assert the client is usable and reports available.
        client = AuditClient(api_key="sk-test")
        self.assertTrue(client.available())

    def test_estimate_cost_unknown_model_uses_fallback(self):
        # unknown model must not price to zero (budget stays conservative)
        cost = cfg.estimate_cost_usd("some-future-model", 1_000_000, 0)
        self.assertGreater(cost, 0)

    def test_estimate_cost_sonnet_5_uses_rate_in_force(self):
        # Sonnet 5 launched with $2/$10 promotional pricing through 2026-08-31,
        # but Anthropic made that price permanent on 2026-08-10.
        before = cfg.estimate_cost_usd("claude-sonnet-5", 1_000_000, 1_000_000,
                                       at="2026-08-15T00:00:00+00:00")
        self.assertAlmostEqual(before, 12.0, places=6)
        after = cfg.estimate_cost_usd("claude-sonnet-5", 1_000_000, 1_000_000,
                                      at="2026-09-01T00:00:00+00:00")
        self.assertAlmostEqual(after, 12.0, places=6)


if __name__ == "__main__":
    unittest.main()
