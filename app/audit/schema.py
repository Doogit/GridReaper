"""Versioned judge prompt + strict output schema + verdict parser (R9.8, R9.9).

The judge is scoped to *objective* checks only (R9.8): does the signal's stored
attribution/classification/evidence/license-play actually hold up against the
raw source text? It never rates usefulness and never proposes changing weights,
half-lives, mappings, or license facts — the request format makes that
impossible (it can only emit the four verdicts below).

Everything the judge produces is versioned (R9.9): ``PROMPT_VERSION`` stamps the
prompt+schema, ``PARSER_VERSION`` stamps this response parser. A change to
either MUST clear the golden set (R9.10) before its verdicts count toward gates.
Bump the version constant whenever you change SYSTEM_PROMPT, OUTPUT_SCHEMA, or
the parse rules — old verdicts keep their old version, so precision reads never
mix judge generations.
"""
import json

# Bump on ANY change to SYSTEM_PROMPT / OUTPUT_SCHEMA / build_judge_input.
PROMPT_VERSION = "judge/1.0"
# Bump on ANY change to parse_response / validate_verdict output shape.
PARSER_VERSION = "audit-parse/1.0"

# The four objective checks (R9.7), in stored order.
CHECKS = ("entity_match", "classification", "evidence_support",
          "license_play_support")

# Per-check verdict values. "not_applicable" covers a check that genuinely
# doesn't apply to this signal (e.g. entity_match on a sector-scope card with no
# entity, or license_play_support on a signal that carries no license play).
# Only pass/fail feed auto-accuracy; unclear/not_applicable are excluded.
RESULTS = ("pass", "fail", "unclear", "not_applicable")

CHECK_QUESTIONS = {
    "entity_match": (
        "Is the signal attributed to the correct watchlist entity? For an "
        "account-scope card the named entity must be the real subject of the "
        "source. For a sector / regulatory-calendar card with no entity, answer "
        "not_applicable."),
    "classification": (
        "Does the assigned trigger type actually describe what the source "
        "reports, and does the headline claim only what the source states "
        "(no over-claim)?"),
    "evidence_support": (
        "Is every quoted evidence snippet genuinely present in, and faithful "
        "to, the raw source text (not paraphrased into a stronger claim)?"),
    "license_play_support": (
        "If the signal carries a license play, is its recommended product/path "
        "a defensible fit for this trigger and entity given the shown facts? If "
        "the signal carries no license play, answer not_applicable."),
}

SYSTEM_PROMPT = (
    "You are an objective QA auditor for a sales-signal system. You are given a "
    "generated signal card and the raw source material it was built from. Your "
    "only job is to check four objective properties of the card against the "
    "source and return a verdict for each.\n\n"
    "Rules:\n"
    "- Judge only what the source supports. Do NOT rate how useful, valuable, "
    "or sales-worthy the signal is — usefulness is out of scope.\n"
    "- Do NOT suggest changing scores, weights, half-lives, product mappings, "
    "or license facts. You only return the four verdicts.\n"
    "- For each check return exactly one of: pass, fail, unclear, "
    "not_applicable. Use not_applicable only when the check does not apply to "
    "this card (e.g. no entity to match, or no license play present). Use "
    "unclear only when the source genuinely does not let you decide.\n"
    "- Keep each note to one sentence citing the specific reason.\n"
    "Return only the structured verdict."
)

# Strict JSON output schema (output_config.format). One object per check; every
# check required; additionalProperties false so the judge cannot smuggle in a
# usefulness score or a recommendation field.
_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"type": "string", "enum": list(RESULTS)},
        "notes": {"type": "string"},
    },
    "required": ["result", "notes"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {check: _CHECK_SCHEMA for check in CHECKS},
    "required": list(CHECKS),
    "additionalProperties": False,
}


def build_judge_input(record):
    """Render the user-message text for one signal (R9.7: signal + raw source
    text + extracted evidence). ``record`` is assembled by the judge runner:

      signal_id, headline, entity_name (or None), signal_scope, trigger_id,
      trigger_name, evidence_quality, incident_evidence_level,
      raw_source_text, evidence [{text, locator}], license_plays
      [{product_name, recommended_path, display_text}]
    """
    lines = ["## Signal card", f"headline: {record.get('headline') or ''}"]
    entity = record.get("entity_name")
    lines.append(f"attributed entity: {entity if entity else '(none — sector/regulatory scope)'}")
    lines.append(f"signal scope: {record.get('signal_scope') or ''}")
    lines.append(
        f"trigger type: {record.get('trigger_name') or record.get('trigger_id') or ''}")
    if record.get("incident_evidence_level"):
        lines.append(f"incident evidence level: {record['incident_evidence_level']}")
    lines.append(f"evidence quality label: {record.get('evidence_quality') or ''}")

    plays = record.get("license_plays") or []
    if plays:
        lines.append("\n## License play(s) on the card")
        for p in plays:
            lines.append(
                f"- product: {p.get('product_name') or ''}; path: "
                f"{p.get('recommended_path') or ''}")
            if p.get("display_text"):
                lines.append(f"  text: {p['display_text']}")
    else:
        lines.append("\n## License play(s) on the card\n(none)")

    lines.append("\n## Extracted evidence snippets (as stored on the card)")
    evidence = record.get("evidence") or []
    if evidence:
        for e in evidence:
            loc = f" [{e.get('locator')}]" if e.get("locator") else ""
            lines.append(f"- \"{e.get('text') or ''}\"{loc}")
    else:
        lines.append("(none)")

    lines.append("\n## Raw source text")
    lines.append(record.get("raw_source_text") or "(unavailable)")

    lines.append("\n## Checks to return")
    for check in CHECKS:
        lines.append(f"- {check}: {CHECK_QUESTIONS[check]}")
    return "\n".join(lines)


def build_request_body(record, model_id, max_tokens):
    """The Anthropic Messages API request body for one signal. Structured
    outputs (output_config.format) force a schema-valid verdict."""
    return {
        "model": model_id,
        "max_tokens": max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_judge_input(record)}],
        "output_config": {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    }


def validate_verdict(obj):
    """Validate a parsed verdict object -> {check: {'result', 'notes'}}.
    Raises ValueError on any missing check or invalid result."""
    if not isinstance(obj, dict):
        raise ValueError("verdict is not a JSON object")
    out = {}
    for check in CHECKS:
        item = obj.get(check)
        if not isinstance(item, dict):
            raise ValueError(f"verdict missing object for check {check!r}")
        result = item.get("result")
        if result not in RESULTS:
            raise ValueError(
                f"check {check!r} has invalid result {result!r}")
        out[check] = {"result": result, "notes": str(item.get("notes") or "")}
    return out


def parse_response(response_json):
    """Parse an Anthropic Messages API response into a validated verdict.
    Returns {check: {'result', 'notes'}}. Raises ValueError if the response is
    not a well-formed judge verdict (the caller records this as an error, never
    as a synthetic pass — R9.12)."""
    content = response_json.get("content")
    if not isinstance(content, list):
        raise ValueError("response has no content list")
    text = "".join(
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") == "text")
    if not text.strip():
        raise ValueError("response carried no text block")
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"judge output was not valid JSON: {exc}")
    return validate_verdict(obj)
