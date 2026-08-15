"""Audit judge configuration and cost model (R9.12, R10.1).

The Claude audit judge is the project's only recurring cost, so every knob that
governs spend is here and env-overridable — the cap can be tightened without a
code change, and secrets stay out of the repo (R10.8). Nothing here reads or
logs the API key; `client.py` reads ``ANTHROPIC_API_KEY`` from the environment
at call time and never logs it.

Budget model: a per-run ceiling in USD (``MAX_USD_PER_RUN``) and a per-run
signal cap (``MAX_SIGNALS_PER_RUN``, the R9.7 min(20, all) bound). When either
is hit the job skips and flags the gap (R9.12) rather than overspending.
"""
import os
from datetime import datetime, timezone

# Anthropic Messages API (called over urllib in client.py; no SDK, R-stdlib).
API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
API_KEY_ENV = "ANTHROPIC_API_KEY"

# Default judge model: a cheap current model is sufficient for the four
# objective checks (R9.7). Haiku 4.5 at $1/$5 per 1M tokens. Overridable for a
# stronger spot check without touching code.
DEFAULT_MODEL_ID = os.environ.get("GRIDSIGNALS_AUDIT_MODEL", "claude-haiku-4-5")

# Per-model price SCHEDULE: each entry is (effective_from, input, output) in USD
# per 1,000,000 tokens, ascending by effective_from. The rate in force at a
# given UTC timestamp is the last entry whose effective_from is <= it, so a
# price change that is already announced can be encoded once and stays correct
# on both sides of its boundary — no code edit on the changeover day, and no
# window where a run is priced at the wrong rate. ``""`` means "from the
# beginning of time". Used only to estimate spend for the budget ceiling and
# audit_runs.budget_spent; an unknown model falls back to the Haiku rate so cost
# is never silently treated as zero.
#
# claude-sonnet-5 is on a promotional $2/$10 through 2026-08-31 and reverts to
# the standard $3/$15 on 2026-09-01. Haiku (the default model, so the only one
# that prices live spend today) and Opus 4.8 have no scheduled change.
PRICING_SCHEDULE_PER_MTOK = {
    "claude-haiku-4-5": [("", 1.00, 5.00)],
    "claude-sonnet-5": [("", 2.00, 10.00), ("2026-09-01", 3.00, 15.00)],
    "claude-opus-4-8": [("", 5.00, 25.00)],
}
_FALLBACK_PRICE = (1.00, 5.00)


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Per-run budget ceiling (R9.12/R10.1). Sized well above one small run
# (5 signals x 4 checks well under a cent at Haiku rates) so a normal weekly
# run never trips it; a runaway loop or a pricier model does.
MAX_USD_PER_RUN = _env_float("GRIDSIGNALS_AUDIT_MAX_USD", 0.50)

# R9.7 sampling cap: min(20, all). Env-overridable for a smaller trial run.
MAX_SIGNALS_PER_RUN = _env_int("GRIDSIGNALS_AUDIT_MAX_SIGNALS", 20)

# Hard per-request timeout (seconds) so one slow call can't wedge the job.
REQUEST_TIMEOUT_S = _env_float("GRIDSIGNALS_AUDIT_TIMEOUT_S", 30.0)

# Output cap for a judge response. Four short verdicts + notes fit easily.
MAX_TOKENS = _env_int("GRIDSIGNALS_AUDIT_MAX_TOKENS", 1024)


def price_per_mtok(model_id, at=None):
    """(input, output) USD per 1,000,000 tokens in force at ``at``.

    ``at`` is a UTC ISO-8601 timestamp (R10.2) and defaults to now — the moment
    the spend is actually incurred. ISO-8601 sorts lexicographically, so a plain
    string compare picks the right schedule row. Unknown model -> Haiku fallback
    rate (never zero) so the budget ceiling stays conservative.
    """
    schedule = PRICING_SCHEDULE_PER_MTOK.get(model_id)
    if not schedule:
        return _FALLBACK_PRICE
    stamp = at or datetime.now(timezone.utc).isoformat()
    price = _FALLBACK_PRICE
    for effective_from, in_rate, out_rate in schedule:
        if effective_from <= stamp:
            price = (in_rate, out_rate)
    return price


def estimate_cost_usd(model_id, input_tokens, output_tokens, at=None):
    """Estimate USD cost for one judge call from token usage, priced at the rate
    in force at ``at`` (UTC ISO-8601; defaults to now). See price_per_mtok."""
    in_rate, out_rate = price_per_mtok(model_id, at)
    return (input_tokens or 0) / 1_000_000 * in_rate + \
           (output_tokens or 0) / 1_000_000 * out_rate
