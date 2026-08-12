"""Anthropic Messages API judge client over urllib (R9.12, R10.8, stdlib-only).

A thin, injectable client: the default transport uses ``urllib`` (no SDK, no
third-party HTTP), and tests pass a fake transport returning canned JSON so no
unit test ever touches the network. It reads ``ANTHROPIC_API_KEY`` from the
environment at call time and NEVER logs the key or the prompt.

Degradation is typed, not exceptional (R9.12): a missing key returns an
``unavailable`` result, an exhausted per-run budget returns ``over_budget``, and
any transport/parse failure returns ``error`` — none of which raise, so the
runner can record a skip and exit cleanly without crashing the pipeline or
inventing synthetic confidence.
"""
import json
import os
import urllib.error
import urllib.request
from collections import namedtuple

from app.audit import config as cfg
from app.audit import schema

# status:
#   ok           -> verdicts populated
#   unavailable  -> no API key (R9.12); do not retry within the run
#   over_budget  -> per-run USD ceiling reached before this call (R9.12)
#   error        -> transport / HTTP / parse failure for this signal
JudgeResult = namedtuple(
    "JudgeResult",
    "status verdicts model_id cost_usd input_tokens output_tokens error")


def _urllib_transport(url, headers, body, timeout):
    """Default transport. Returns (status_code, response_bytes). Raises on
    connection-level failures (caller contains them). HTTP error responses are
    returned with their status so the caller can surface the API's message."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class AuditClient:
    """Capped, injectable judge client. One instance per audit run tracks
    cumulative spend so the per-run budget ceiling is enforced across calls."""

    def __init__(self, api_key=None, transport=None, model_id=None,
                 max_usd=None, timeout=None, max_tokens=None):
        # api_key defaults to the env var and is never logged. An explicit
        # empty/None key means "no key" -> the run degrades to a skip.
        self._api_key = api_key if api_key is not None else \
            os.environ.get(cfg.API_KEY_ENV)
        self._transport = transport or _urllib_transport
        self.model_id = model_id or cfg.DEFAULT_MODEL_ID
        self.max_usd = cfg.MAX_USD_PER_RUN if max_usd is None else max_usd
        self.timeout = cfg.REQUEST_TIMEOUT_S if timeout is None else timeout
        self.max_tokens = cfg.MAX_TOKENS if max_tokens is None else max_tokens
        self.spent_usd = 0.0
        self.calls = 0

    def available(self):
        """True when a non-empty API key is present."""
        return bool(self._api_key and str(self._api_key).strip())

    def over_budget(self):
        """True when the per-run USD ceiling has already been reached."""
        return self.spent_usd >= self.max_usd

    def _headers(self):
        # x-api-key carries the secret; it is set here and never logged.
        return {
            "content-type": "application/json",
            "anthropic-version": cfg.ANTHROPIC_VERSION,
            "x-api-key": self._api_key,
        }

    def judge(self, record):
        """Audit one signal record. Returns a JudgeResult; never raises."""
        if not self.available():
            return JudgeResult("unavailable", None, self.model_id,
                               0.0, 0, 0, "ANTHROPIC_API_KEY not set")
        if self.over_budget():
            return JudgeResult("over_budget", None, self.model_id, 0.0, 0, 0,
                               f"per-run budget ${self.max_usd:.4f} reached")

        body = json.dumps(
            schema.build_request_body(record, self.model_id, self.max_tokens)
        ).encode("utf-8")
        try:
            status_code, raw = self._transport(
                cfg.API_URL, self._headers(), body, self.timeout)
        except Exception as exc:  # noqa: BLE001 - contain any transport failure
            # The exception cannot contain the key (headers are not echoed by
            # urllib errors); still, we surface only the type + message.
            return JudgeResult("error", None, self.model_id, 0.0, 0, 0,
                               f"transport {type(exc).__name__}: {exc}")

        try:
            response_json = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return JudgeResult("error", None, self.model_id, 0.0, 0, 0,
                               f"non-JSON response (HTTP {status_code}): {exc}")

        if status_code != 200:
            api_err = ""
            if isinstance(response_json, dict):
                api_err = (response_json.get("error") or {}).get("message", "")
            return JudgeResult("error", None, self.model_id, 0.0, 0, 0,
                               f"HTTP {status_code}: {api_err}")

        usage = response_json.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        cost = cfg.estimate_cost_usd(self.model_id, in_tok, out_tok)
        # Charge the run before parsing: a call we paid for counts against the
        # budget even if its output turns out unparseable.
        self.spent_usd += cost
        self.calls += 1

        try:
            verdicts = schema.parse_response(response_json)
        except ValueError as exc:
            return JudgeResult("error", None, self.model_id, cost,
                               in_tok, out_tok, f"parse error: {exc}")

        return JudgeResult("ok", verdicts, self.model_id, cost,
                           in_tok, out_tok, "")
