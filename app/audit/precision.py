"""Precision computation for the Feedback / Precision view (R9.3, R9.4, R9.5).

PURE computations. No network, no Streamlit. Every computation function takes
lists of plain dict rows (fetched and shaped by the UI layer) and returns plain
dicts / lists. Any date/age reasoning accepts an injectable ``now`` so callers
and tests stay deterministic; all timestamps are UTC ISO-8601 (R10.2).

The ONE piece of I/O in this file is the ``--report`` CLI at the bottom —
R9.3's monthly job. It opens a connection, reads its rows through
``app.ui.data.precision_report`` and prints what the functions above compute; it
adds no computation of its own and writes nothing to the store. It is named
``--report``, not ``--materialize``, because it prints: in this repo
"materialize" means a durable write (``app.obligations``), and the monthly
record is deliberately the LOG LINE, not a new table.

TRUST INVARIANT (returning-maintainer rule): a rate is *never* returned as a
bare percentage. Every computed rate ships alongside its ``n`` (denominator),
and a rate over an empty denominator is ``None`` — not ``0.0``, not a fake
gauge. Read a ``None`` rate as "no evidence yet", never as "0% good".

What each metric means:

- R9.3 precision per trigger / source / signal_scope: the human useful-rate
  (positives over rated cards) sliced by dimension, plus the automated judge
  accuracy (pass over pass+fail on the two objective checks entity_match +
  evidence_support) sliced the same way.
- R9.4 Gate G1 (Stage 1 -> 2): on BOTH primary MVP account-level trigger types,
  human useful-rate >= 60% AND auto-accuracy >= 80%, over >= 30 days AND >= 20
  rated account-specific cards. Sector-wide regulatory-calendar cards and
  unconfirmed early-warning incident cards are computed SEPARATELY (returned
  under ``reported_separately``) so they can never inflate or hide weak
  account-level precision.
- R9.5 Gate G2 (per source): < 40% precision over the window triggers a
  *recommendation* to demote a source to store-only / disable it, but only
  after checking sample size and reason-code distribution. This module is
  REPORT-ONLY: it recommends, it never enforces.

Reason codes (R9.2) are the ten in ``app.ui.data.REASON_CODES``; verdicts
(R9.1) are the three in ``app.ui.data.VERDICTS``. "useful" OR "converted" is a
positive; "not_useful" is a negative. The objective judge checks and results
mirror ``app.audit.schema`` (CHECKS / RESULTS); only entity_match +
evidence_support feed auto-accuracy, and only "pass"/"fail" are scored
("unclear"/"not_applicable" are excluded from the denominator).
"""
import argparse
import math
import sys
from datetime import datetime, timedelta, timezone

from app.db.connection import get_connection

# Verdicts counted as a human positive vs negative (R9.1).
POSITIVE_VERDICTS = frozenset({"useful", "converted"})
NEGATIVE_VERDICTS = frozenset({"not_useful"})

# Only these two objective checks feed auto-accuracy (R9.4); only these two
# results are scored ("unclear"/"not_applicable" excluded from the denominator).
AUTO_ACCURACY_CHECKS = frozenset({"entity_match", "evidence_support"})
SCORED_RESULTS = frozenset({"pass", "fail"})
CORRECT_RESULT = "pass"

# Signal scopes that count as an "account-specific" card for G1. Everything
# else (sector / subsector / regulatory_calendar, and unconfirmed early-warning
# incident cards) is reported separately and never counts toward the account
# trigger's precision. Mirrors app.ui.data.SCOPE_GROUPS["account"].
ACCOUNT_SCOPES = frozenset({"account", "parent"})

# The two primary MVP account-level trigger types the G1 gate is measured on.
DEFAULT_PRIMARY_TRIGGERS = ("leadership_change", "nerc_enforcement")

# The dimensions R9.3 slices precision by, and the row key that identifies each.
DIMENSIONS = ("trigger", "source", "signal_scope", "incident_evidence_level")
DIMENSION_KEYS = {
    "trigger": "trigger_id",
    "source": "source_id",
    "signal_scope": "signal_scope",
    "incident_evidence_level": "incident_evidence_level",
}

# The three dimensions R9.3's monthly job reports on, verbatim from the
# requirement ("precision per trigger type, source, and signal_scope"). It is a
# subset of DIMENSIONS: incident_evidence_level is a Precision-page slice R9.3
# does not ask the monthly record to carry.
REPORT_DIMENSIONS = ("trigger", "source", "signal_scope")

# G1 thresholds (R9.4): fixed by the PRD, exposed as parameters so tests pin
# them and the caller can override for what-if analysis.
G1_USEFUL_RATE_MIN = 0.60
G1_AUTO_ACCURACY_MIN = 0.80
G1_MIN_DAYS = 30
G1_MIN_RATED = 20

# R9.4 Gate G1 operator waiver — the RECORD of the ruling lives here (KTD1).
#
# G1 needs >= 20 rated account-specific cards over >= 30 days. There have never
# been any account-specific cards and never any ratings, so the gate is
# unreachable, not slow. The operator waived it by explicit ruling on
# ``G1_WAIVER_DATE``.
#
# The waiver is a DISCLOSURE, never a pass: ``g1_status`` reports a distinct
# ``"waived"`` state that no computation can produce, ``eligible`` keeps its
# real (False) computed value, and the useful-rate / auto-accuracy keep
# computing and reporting their real denominators underneath. A product whose
# trust posture is "every rate shown with its sample size" must not display a
# gate as met on a denominator of zero.
#
# ``G1_WAIVER_LIFT_CONDITION`` records what ENDS the waiver. It is recorded, not
# enforced: whether the lift is automatic or a hand ruling is an open question,
# so ``g1_status`` never falls back to the computed state on its own.
G1_WAIVER_ACTIVE = True
G1_WAIVER_DATE = "2026-08-16"
G1_WAIVER_REASON = (
    f"Overall: WAIVED by operator ruling {G1_WAIVER_DATE} — not passed. The "
    "gate's own conditions below are unmet and are shown unchanged.")
G1_WAIVER_LIFT_CONDITION = (
    f"spent once any primary account trigger reaches {G1_MIN_RATED} rated "
    "cards; from then on the real computed state governs")

# G2 thresholds (R9.5). ``G2_THRESHOLD`` is the PRD's 40% precision floor.
# ``G2_MIN_N`` is our sample-size floor for *recommending* a demotion: below
# it, precision is too noisy to act on, so demote_recommended stays False with
# a "sample too small" note. We reuse the G1 rated-card floor (20) — the same
# "one month of real feedback" bar the PRD sets for account precision — because
# the PRD gives no separate number and demoting a source is a real action that
# deserves at least that much evidence.
G2_THRESHOLD = 0.40
G2_MIN_N = 20


def _dimension_key(dimension):
    """Map a public dimension name to the row key that carries its value."""
    try:
        return DIMENSION_KEYS[dimension]
    except KeyError:
        raise ValueError(
            f"unknown dimension {dimension!r}; expected one of {DIMENSIONS}")


def _rate(numerator, denominator):
    """Guarded rate: ``None`` over an empty denominator, else the float ratio."""
    if denominator == 0:
        return None
    return numerator / denominator


def _parse_ts(value):
    """Parse a UTC ISO-8601 timestamp/date into an aware datetime, or None."""
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now(now):
    """Normalize an injected ``now`` (datetime or ISO string) to aware UTC."""
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    parsed = _parse_ts(now)
    return parsed if parsed is not None else datetime.now(timezone.utc)


def _is_account_scope(row):
    """True when a row is an account-specific card (entity present + scope).

    Unconfirmed early-warning incidents never count toward account precision
    even when account-scoped with an entity (R8.6/R9.4): they are unverified,
    so they are reported separately and can neither inflate nor hide the
    account trigger's real precision. Confirmed/corroborated own incidents,
    like any other account card, still count."""
    if row.get("incident_evidence_level") == "unconfirmed_early_warning":
        return False
    scope = row.get("signal_scope")
    if scope not in ACCOUNT_SCOPES:
        return False
    return row.get("entity_id") is not None


def useful_rate_overall(feedback_rows):
    """Human useful-rate over all rated feedback rows.

    Returns ``{"useful": int, "total": int, "rate": float|None}`` where a
    positive is verdict in {useful, converted}, total counts every row with a
    positive-or-negative verdict, and ``rate`` is ``None`` when total == 0.
    """
    useful = total = 0
    for row in feedback_rows:
        verdict = row.get("verdict")
        if verdict in POSITIVE_VERDICTS:
            useful += 1
            total += 1
        elif verdict in NEGATIVE_VERDICTS:
            total += 1
    return {"useful": useful, "total": total, "rate": _rate(useful, total)}


def useful_rate(feedback_rows, dimension):
    """Human useful-rate sliced by ``dimension`` (R9.3).

    Returns ``{value: {"useful": int, "total": int, "rate": float|None}}`` with
    one entry per distinct dimension value, plus an ``"overall"`` entry. A
    positive is verdict in {useful, converted}; ``rate`` is ``None`` when that
    value has no rated rows. Output is key-sorted for determinism.
    """
    key = _dimension_key(dimension)
    buckets = {}
    for row in feedback_rows:
        verdict = row.get("verdict")
        if verdict not in POSITIVE_VERDICTS and verdict not in NEGATIVE_VERDICTS:
            continue
        value = row.get(key)
        bucket = buckets.setdefault(value, {"useful": 0, "total": 0})
        bucket["total"] += 1
        if verdict in POSITIVE_VERDICTS:
            bucket["useful"] += 1
    out = {}
    for value in sorted(buckets, key=lambda v: (v is None, v)):
        b = buckets[value]
        out[value] = {
            "useful": b["useful"],
            "total": b["total"],
            "rate": _rate(b["useful"], b["total"]),
        }
    out["overall"] = useful_rate_overall(feedback_rows)
    return out


def auto_accuracy(audit_rows, dimension):
    """Automated judge accuracy sliced by ``dimension`` (R9.3, R9.4).

    Uses ONLY entity_match + evidence_support checks; correct = result "pass",
    scored = pass + fail ("unclear"/"not_applicable" excluded). Returns
    ``{value: {"correct": int, "scored": int, "accuracy": float|None}}`` with an
    ``"overall"`` entry; ``accuracy`` is ``None`` when scored == 0.
    """
    key = _dimension_key(dimension)
    buckets = {}
    overall = {"correct": 0, "scored": 0}
    for row in audit_rows:
        if row.get("check_type") not in AUTO_ACCURACY_CHECKS:
            continue
        result = row.get("result")
        if result not in SCORED_RESULTS:
            continue
        value = row.get(key)
        bucket = buckets.setdefault(value, {"correct": 0, "scored": 0})
        bucket["scored"] += 1
        overall["scored"] += 1
        if result == CORRECT_RESULT:
            bucket["correct"] += 1
            overall["correct"] += 1
    out = {}
    for value in sorted(buckets, key=lambda v: (v is None, v)):
        b = buckets[value]
        out[value] = {
            "correct": b["correct"],
            "scored": b["scored"],
            "accuracy": _rate(b["correct"], b["scored"]),
        }
    out["overall"] = {
        "correct": overall["correct"],
        "scored": overall["scored"],
        "accuracy": _rate(overall["correct"], overall["scored"]),
    }
    return out


def reason_code_distribution(feedback_rows):
    """Reason-code counts across not_useful rows (R9.2), desc by count.

    Returns an ordered list of ``{"reason_code": code, "count": n}`` for every
    reason code that appears on a not_useful row, sorted by count desc then
    code asc (stable, deterministic). Positive verdicts carry no reason code
    and are ignored.
    """
    counts = {}
    for row in feedback_rows:
        if row.get("verdict") not in NEGATIVE_VERDICTS:
            continue
        code = row.get("reason_code")
        if not code:
            continue
        counts[code] = counts.get(code, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"reason_code": code, "count": n} for code, n in ordered]


def _judge_negative_signals(audit_rows):
    """Signal ids the judge flagged negative on an objective check.

    Mapping: a judge-negative is any signal where entity_match OR
    evidence_support returned "fail". These are the two objective checks that
    feed auto-accuracy (R9.4), so a "fail" on either is the judge's strongest
    objective concern about the card. "unclear"/"not_applicable"/"pass" do not
    make a signal judge-negative. A signal the judge only ever passed (on these
    checks) is judge-positive.
    """
    seen = set()
    negative = set()
    for row in audit_rows:
        if row.get("check_type") not in AUTO_ACCURACY_CHECKS:
            continue
        sid = row.get("signal_id")
        seen.add(sid)
        if row.get("result") == "fail":
            negative.add(sid)
    return seen, negative


def judge_human_disagreement(audit_rows, feedback_rows):
    """Judge-vs-human agreement over signals that have BOTH sides (QA signal).

    This is a QA signal, NOT ground truth (the judge rates objective facts, the
    human rates usefulness — they measure different things, so some disagreement
    is expected and healthy).

    Mapping (documented, defensible, deliberately coarse):
      * judge verdict per signal: "negative" if entity_match OR evidence_support
        returned "fail" on that signal; otherwise "positive". A signal with no
        entity_match/evidence_support judge row is not comparable (no judge
        side).
      * human verdict per signal: "negative" if the human marked not_useful;
        "positive" if the human marked useful/converted. A signal with no human
        feedback is not comparable (no human side).
      * agree = both negative or both positive; disagree otherwise.

    Only signals with BOTH a judge side and a human side are comparable. Returns
    ``{"comparable": n, "agree": n, "disagree": n, "disagreement_rate":
    float|None, "items": [{signal_id, judge, human}, ...]}`` sorted by
    signal_id; ``disagreement_rate`` is ``None`` when comparable == 0.
    """
    judged, judge_neg = _judge_negative_signals(audit_rows)

    human = {}  # signal_id -> "positive"/"negative"; last verdict wins
    for row in feedback_rows:
        verdict = row.get("verdict")
        if verdict in POSITIVE_VERDICTS:
            human[row.get("signal_id")] = "positive"
        elif verdict in NEGATIVE_VERDICTS:
            human[row.get("signal_id")] = "negative"

    items = []
    agree = disagree = 0
    for sid in sorted(judged & human.keys(), key=lambda s: (s is None, s)):
        judge_verdict = "negative" if sid in judge_neg else "positive"
        human_verdict = human[sid]
        if judge_verdict == human_verdict:
            agree += 1
        else:
            disagree += 1
        items.append(
            {"signal_id": sid, "judge": judge_verdict, "human": human_verdict})
    comparable = len(items)
    return {
        "comparable": comparable,
        "agree": agree,
        "disagree": disagree,
        "disagreement_rate": _rate(disagree, comparable),
        "items": items,
    }


# R9.11 per-source disagreement gate + spot-check coverage.
#
# KTD3 (operator-ratified reconciliation): R9.11 literally says high judge-human
# disagreement "blocks judge verdicts from demotion." In THIS build the only
# demotion signal (G2 ``demote_recommended``) is computed from the HUMAN
# useful-rate, and the judge is report-only (R9.8) — there are no judge verdicts
# flowing into demotion. So we treat >20% per-source disagreement as a trigger to
# SUPPRESS the G2 demote recommendation for that source and flag it, rather than
# gating a (non-existent) judge->demotion path. Report-only (R9.5); the judge
# never gates or tunes (R9.8).
DISAGREEMENT_GATE_THRESHOLD = 0.20   # ">20% disagreement" per R9.11
# Sample floor for TRUSTING a per-source disagreement rate enough to act on it.
# Below it, the rate is too noisy to justify withholding a human-derived
# recommendation, so the gate reads "below floor" — never a fabricated block. We
# reuse the G2 sample floor so the gate demands the same evidence bar that the
# recommendation it overrides does.
DISAGREEMENT_MIN_COMPARABLE = G2_MIN_N

# R9.11 spot-check coverage: >= 10 human-reviewed audit verdicts per month, OR
# 20% of that month's audited signals, whichever is smaller — with a hard floor
# of 5.
SPOTCHECK_ABS_TARGET = 10
SPOTCHECK_FRACTION = 0.20
SPOTCHECK_MIN_FLOOR = 5

GATE_WITHHELD_NOTE = (
    "judge verdicts withheld from demotion — revise rubric")


def _month_key(value):
    """UTC calendar-month key ``(year, month)`` for an ISO ts, or None.

    WINDOW DECISION (Open Q#2, default): the spot-check window is a UTC CALENDAR
    month. The operator may prefer a trailing-30-day window instead — flagged,
    not silently chosen.
    """
    dt = _parse_ts(value)
    if dt is None:
        return None
    return (dt.year, dt.month)


def judge_human_disagreement_by_source(audit_rows, feedback_rows):
    """Per-source judge-vs-human disagreement (R9.11, KTD3).

    Same comparability rule as ``judge_human_disagreement`` (a signal is
    comparable only when it has BOTH a judge side — an entity_match/
    evidence_support verdict — AND a human side), but bucketed by the signal's
    ``source_id``. Source is resolved from the AUDIT row's ``source_id`` (which
    ``app.ui.data.precision_audit_rows`` fills via the raw_events JOIN), exactly
    as the other per-source metrics do; a signal with no audit source falls in
    the ``None`` bucket.

    Trust invariant: every rate ships with its ``comparable`` count, and a rate
    over an empty denominator is ``None`` — the caller reads ``comparable`` to
    tell dormant (0 comparable) from active. Returns
    ``{source_id: {"comparable", "agree", "disagree", "disagreement_rate"}}``
    sorted by source_id.
    """
    judged, judge_neg = _judge_negative_signals(audit_rows)

    # signal_id -> source_id (from the audit side, where the join lives). Last
    # non-None wins, so a signal's source is stable across its audit rows.
    source_of = {}
    for row in audit_rows:
        if row.get("check_type") not in AUTO_ACCURACY_CHECKS:
            continue
        sid = row.get("signal_id")
        src = row.get("source_id")
        if src is not None or sid not in source_of:
            source_of[sid] = src

    human = {}  # signal_id -> "positive"/"negative"; last verdict wins
    for row in feedback_rows:
        verdict = row.get("verdict")
        if verdict in POSITIVE_VERDICTS:
            human[row.get("signal_id")] = "positive"
        elif verdict in NEGATIVE_VERDICTS:
            human[row.get("signal_id")] = "negative"

    buckets = {}
    for sid in judged & human.keys():
        source = source_of.get(sid)
        b = buckets.setdefault(source, {"comparable": 0, "agree": 0,
                                        "disagree": 0})
        judge_verdict = "negative" if sid in judge_neg else "positive"
        b["comparable"] += 1
        if judge_verdict == human[sid]:
            b["agree"] += 1
        else:
            b["disagree"] += 1

    out = {}
    for source in sorted(buckets, key=lambda s: (s is None, s)):
        b = buckets[source]
        out[source] = {
            "comparable": b["comparable"],
            "agree": b["agree"],
            "disagree": b["disagree"],
            "disagreement_rate": _rate(b["disagree"], b["comparable"]),
        }
    return out


def g2_gated(g2_result, disagreement_by_source,
             threshold=DISAGREEMENT_GATE_THRESHOLD,
             min_comparable=DISAGREEMENT_MIN_COMPARABLE):
    """Overlay G2 with the R9.11 disagreement gate (KTD3) — PURE, non-mutating.

    Returns a NEW dict (the input ``g2_result`` is never mutated) shaped exactly
    like ``g2_status`` output but with two added keys per source:
      * ``gate``: one of ``"withheld"`` (disagreement > threshold with enough
        comparable evidence — demotion suppressed), ``"below_floor"`` (comparable
        < ``min_comparable``, so the rate is too noisy to trust — NO block),
        ``"na"`` (no comparable judge∩human signals for this source), or ``"ok"``
        (enough comparable evidence, disagreement at/below threshold).
      * ``gate_note``: human copy for the gate state (``None`` when ``ok``).
    Plus ``disagreement_rate`` and ``comparable`` (always carried, so the UI can
    show the rate WITH its n — a reachability readout).

    When ``gate == "withheld"`` the copy of that source's cell has
    ``demote_recommended`` forced to False and its ``note`` prefixed with the
    withheld flag — so a noisy judge can veto a human-derived recommendation, but
    only when the disagreement rate is itself well-evidenced (comparable >=
    floor). Below the floor the gate NEVER blocks: honest low-n, not a fabricated
    gate.

    ``disagreement_by_source`` is the output of
    ``judge_human_disagreement_by_source``.
    """
    out = {}
    for sid, cell in g2_result.items():
        new_cell = dict(cell)
        dis = disagreement_by_source.get(sid)
        comparable = dis["comparable"] if dis else 0
        rate = dis["disagreement_rate"] if dis else None
        new_cell["comparable"] = comparable
        new_cell["disagreement_rate"] = rate
        if comparable == 0 or rate is None:
            new_cell["gate"] = "na"
            new_cell["gate_note"] = (
                "no comparable judge∩human signals for this source — "
                "gate n/a")
        elif comparable < min_comparable:
            new_cell["gate"] = "below_floor"
            new_cell["gate_note"] = (
                f"disagreement {rate:.0%} over only {comparable} comparable "
                f"(<{min_comparable}) — too few to gate; not blocking")
        elif rate > threshold and cell["demote_recommended"]:
            new_cell["gate"] = "withheld"
            new_cell["gate_note"] = (
                f"{GATE_WITHHELD_NOTE} (disagreement {rate:.0%}>"
                f"{threshold:.0%} over {comparable} comparable)")
            new_cell["demote_recommended"] = False
            # Replace (not prefix) the base note so no residual "consider
            # store-only/disable" demote guidance survives the suppression.
            new_cell["note"] = (
                f"{GATE_WITHHELD_NOTE} — base precision was "
                f"{cell['precision']:.0%}<{G2_THRESHOLD:.0%} over n={cell['n']}, "
                "but judge-human disagreement is too high to act on it")
        else:
            new_cell["gate"] = "ok"
            new_cell["gate_note"] = None
        out[sid] = new_cell
    return out


def spotcheck_coverage(audit_rows, feedback_rows, now=None,
                       abs_target=SPOTCHECK_ABS_TARGET,
                       fraction=SPOTCHECK_FRACTION,
                       floor=SPOTCHECK_MIN_FLOOR):
    """Monthly audit spot-check coverage (R9.11) — report-only.

    R9.11 asks for >= 10 human-reviewed audit verdicts per month OR 20% of that
    month's audited signals (whichever is smaller), with a hard floor of 5.

    STORAGE (Open Q#2 decision): reuses the EXISTING human-feedback-on-audited-
    signal rows — no new table. A "spot-check" is a human rating a signal that
    was audited; that act is already a ``feedback`` row on an audited signal.
      * ``reviewed`` (numerator) = distinct signals AUDITED this month
        (entity_match/evidence_support verdict, ``audit.ts`` in the month) that
        also have a human ``feedback`` verdict in the same month — i.e. a human
        reviewed the audited signal inside the reported window.
      * ``audited`` (denominator for the 20% branch) = distinct signals audited
        this month (distinct audited signal_id — named source table: the ``audit``
        rows / ``precision_audit_rows``).
    The month is the calendar month containing ``now`` (UTC; see ``_month_key``).

    ``target`` = ``max(floor, min(abs_target, ceil(fraction * audited)))``.
    ``met`` is True when ``reviewed >= target``. Trust invariant: ``reviewed``,
    ``audited`` and ``target`` all ship together. Returns
    ``{"reviewed", "audited", "target", "met", "window"}`` where ``window`` is
    the ``"YYYY-MM"`` UTC month.
    """
    now_dt = _now(now)
    month = (now_dt.year, now_dt.month)

    audited = set()
    for row in audit_rows:
        if row.get("check_type") not in AUTO_ACCURACY_CHECKS:
            continue
        if _month_key(row.get("ts")) != month:
            continue
        audited.add(row.get("signal_id"))

    human_signals = set()
    for row in feedback_rows:
        verdict = row.get("verdict")
        if verdict not in POSITIVE_VERDICTS and verdict not in NEGATIVE_VERDICTS:
            continue
        if _month_key(row.get("ts")) != month:
            continue
        human_signals.add(row.get("signal_id"))

    reviewed = len(audited & human_signals)
    audited_n = len(audited)
    frac_target = math.ceil(fraction * audited_n) if audited_n else 0
    target = max(floor, min(abs_target, frac_target)) if audited_n else floor
    return {
        "reviewed": reviewed,
        "audited": audited_n,
        "target": target,
        "met": reviewed >= target,
        "window": f"{month[0]:04d}-{month[1]:02d}",
    }


def half_life_effectiveness(halflife_rows):
    """Per-trigger view of configured half-life vs observed feedback.

    For each trigger it relates the configured ``decay_half_life_days`` to the
    observed human useful-rate and the mean stored ``score_decay`` on that
    trigger's cards, so the operator can eyeball whether the half-life looks
    well-calibrated (e.g. a short half-life paired with a still-high useful-rate
    on aged cards suggests the decay may be too aggressive). This is a
    descriptive aid, not a recommendation — no threshold is applied.

    Returns a list (sorted by trigger_id) of per-trigger dicts, each carrying
    its own sample sizes:
      ``{"trigger_id", "trigger_name", "decay_half_life_days",
         "cards": int, "useful": int, "rated": int,
         "useful_rate": float|None, "mean_score_decay": float|None,
         "decay_samples": int}``
    ``useful_rate`` is over rated cards only (verdict present); it is ``None``
    when no card on the trigger has feedback. ``mean_score_decay`` averages the
    non-null ``score_decay`` values (``decay_samples`` of them) and is ``None``
    when none are present.
    """
    buckets = {}
    for row in halflife_rows:
        tid = row.get("trigger_id")
        b = buckets.get(tid)
        if b is None:
            b = buckets[tid] = {
                "trigger_id": tid,
                "trigger_name": row.get("trigger_name"),
                "decay_half_life_days": row.get("decay_half_life_days"),
                "cards": 0,
                "useful": 0,
                "rated": 0,
                "decay_sum": 0.0,
                "decay_samples": 0,
            }
        b["cards"] += 1
        verdict = row.get("verdict")
        if verdict in POSITIVE_VERDICTS:
            b["useful"] += 1
            b["rated"] += 1
        elif verdict in NEGATIVE_VERDICTS:
            b["rated"] += 1
        decay = row.get("score_decay")
        if decay is not None:
            b["decay_sum"] += decay
            b["decay_samples"] += 1
    out = []
    for tid in sorted(buckets, key=lambda t: (t is None, t)):
        b = buckets[tid]
        mean_decay = (
            b["decay_sum"] / b["decay_samples"] if b["decay_samples"] else None)
        out.append({
            "trigger_id": b["trigger_id"],
            "trigger_name": b["trigger_name"],
            "decay_half_life_days": b["decay_half_life_days"],
            "cards": b["cards"],
            "useful": b["useful"],
            "rated": b["rated"],
            "useful_rate": _rate(b["useful"], b["rated"]),
            "mean_score_decay": mean_decay,
            "decay_samples": b["decay_samples"],
        })
    return out


def _days_span(timestamps, now):
    """Span in days from the earliest timestamp to ``now`` (0 if none/future)."""
    parsed = [t for t in (_parse_ts(ts) for ts in timestamps) if t is not None]
    if not parsed:
        return 0
    earliest = min(parsed)
    delta = (now - earliest).total_seconds() / 86400.0
    return max(delta, 0.0)


def g1_status(feedback_rows, audit_rows, primary_triggers=None, now=None,
              min_days=G1_MIN_DAYS, min_rated=G1_MIN_RATED,
              waiver_active=None):
    """Gate G1 (Stage 1 -> 2) status per primary account-level trigger (R9.4).

    ONLY account-specific rated cards (entity_id present AND signal_scope in
    {account, parent}) count toward each primary trigger's precision. Sector /
    subsector / regulatory_calendar cards and unconfirmed early-warning incident
    cards are computed SEPARATELY and returned under ``reported_separately`` so
    they never inflate or hide account-level precision.

    ``meets`` for a trigger requires ALL of: useful_rate >= 0.60,
    auto_accuracy >= 0.80, days_span >= ``min_days``, useful_n >= ``min_rated``.
    When the sample is too small (or a rate is ``None``), ``meets`` is False and
    ``reasons`` names why — never a fake pass.

    ``primary_triggers`` defaults to the two MVP account triggers; pass a list
    to override. Returns::

        {
          "triggers": {trigger_id: {"useful_rate", "useful_n", "auto_accuracy",
                                    "auto_n", "days_span", "meets", "reasons"}},
          "reported_separately": {"feedback": {...useful_rate_overall shape...},
                                  "auto": {...auto overall shape...}},
          "eligible": bool,
          "blocked_reasons": [...],
          "state": "waived" | "eligible" | "blocked",
          "waiver": {"date", "reason", "lift_condition"} | None,
        }

    ``eligible`` is True only when every primary trigger ``meets``.

    THE WAIVER (KTD1). ``waiver_active`` defaults to ``None``, resolved in the
    body to the recorded ``G1_WAIVER_ACTIVE`` ruling — read at CALL time, the
    same way the three other waiver constants are, so the documented rollback
    (edit the constant) and a ``mock.patch.object`` of it both take effect. A
    default of ``G1_WAIVER_ACTIVE`` would bind the ruling once at import and
    freeze one of the four constants while the other three stayed live. When
    ``waiver_active`` is true, ``state`` is ``"waived"`` and ``waiver``
    carries the ruling date, its one-line reason, and the condition that would
    end it. ``"waived"`` is NOT reachable by computation — only by the recorded
    waiver — and it is not a pass: ``eligible``, every per-trigger cell, and
    ``blocked_reasons`` all keep their real computed values so the gate's unmet
    conditions stay visible underneath the waiver. The lift condition is
    RECORDED, not ENFORCED: a waived gate stays waived even once a trigger
    reaches ``min_rated``.
    """
    now_dt = _now(now)
    if waiver_active is None:
        waiver_active = G1_WAIVER_ACTIVE
    triggers = list(primary_triggers) if primary_triggers else list(
        DEFAULT_PRIMARY_TRIGGERS)

    # Partition rows into account-specific vs reported-separately.
    account_fb = [r for r in feedback_rows if _is_account_scope(r)]
    other_fb = [r for r in feedback_rows if not _is_account_scope(r)]
    account_audit = [r for r in audit_rows if _is_account_scope(r)]
    other_audit = [r for r in audit_rows if not _is_account_scope(r)]

    per_trigger = {}
    for tid in triggers:
        fb = [r for r in account_fb if r.get("trigger_id") == tid]
        au = [r for r in account_audit if r.get("trigger_id") == tid]
        ur = useful_rate_overall(fb)
        aa = auto_accuracy(au, "trigger")["overall"]
        rated_ts = [
            r.get("ts") for r in fb
            if r.get("verdict") in POSITIVE_VERDICTS
            or r.get("verdict") in NEGATIVE_VERDICTS]
        days_span = _days_span(rated_ts, now_dt)

        reasons = []
        useful_rate_value = ur["rate"]
        useful_n = ur["total"]
        auto_accuracy_value = aa["accuracy"]
        auto_n = aa["scored"]

        if useful_n < min_rated:
            reasons.append(f"n<{min_rated} rated account cards (have {useful_n})")
        if days_span < min_days:
            reasons.append(
                f"span<{min_days}d (have {days_span:.1f}d)")
        if useful_rate_value is None:
            reasons.append("no rated account cards for useful-rate")
        elif useful_rate_value < G1_USEFUL_RATE_MIN:
            reasons.append(
                f"useful-rate {useful_rate_value:.0%}<{G1_USEFUL_RATE_MIN:.0%}")
        if auto_accuracy_value is None:
            reasons.append("no scored judge checks for auto-accuracy")
        elif auto_accuracy_value < G1_AUTO_ACCURACY_MIN:
            reasons.append(
                f"auto-accuracy {auto_accuracy_value:.0%}"
                f"<{G1_AUTO_ACCURACY_MIN:.0%}")

        meets = (
            useful_rate_value is not None
            and useful_rate_value >= G1_USEFUL_RATE_MIN
            and auto_accuracy_value is not None
            and auto_accuracy_value >= G1_AUTO_ACCURACY_MIN
            and days_span >= min_days
            and useful_n >= min_rated)

        per_trigger[tid] = {
            "useful_rate": useful_rate_value,
            "useful_n": useful_n,
            "auto_accuracy": auto_accuracy_value,
            "auto_n": auto_n,
            "days_span": days_span,
            "meets": meets,
            "reasons": reasons,
        }

    blocked_reasons = []
    for tid in triggers:
        if not per_trigger[tid]["meets"]:
            for reason in per_trigger[tid]["reasons"]:
                blocked_reasons.append(f"{tid}: {reason}")
    eligible = all(per_trigger[tid]["meets"] for tid in triggers)

    if waiver_active:
        state = "waived"
        waiver = {
            "date": G1_WAIVER_DATE,
            "reason": G1_WAIVER_REASON,
            "lift_condition": G1_WAIVER_LIFT_CONDITION,
        }
    else:
        state = "eligible" if eligible else "blocked"
        waiver = None

    return {
        "triggers": per_trigger,
        "reported_separately": {
            "feedback": useful_rate_overall(other_fb),
            "auto": auto_accuracy(other_audit, "trigger")["overall"],
        },
        "eligible": eligible,
        "blocked_reasons": blocked_reasons,
        "state": state,
        "waiver": waiver,
    }


def g2_status(feedback_rows, threshold=G2_THRESHOLD, min_n=G2_MIN_N, now=None):
    """Gate G2 (per source) demotion *recommendation* — REPORT-ONLY (R9.5).

    G2 is reported here, never auto-enforced: this function only recommends. Per
    source it computes precision = human useful-rate, the rated count ``n``, and
    the reason-code distribution over that source's not_useful rows (so an
    operator can see WHY before demoting). A demotion is recommended only when
    precision < ``threshold`` AND ``n`` >= ``min_n``; when ``n`` is below
    ``min_n`` the sample is too noisy to act on, so ``demote_recommended`` is
    False with a "sample too small" note.

    ``now`` is accepted for signature symmetry / future window filtering; the
    caller is expected to have already windowed rows to the 30-day period.
    Returns ``{source_id: {"precision", "n", "reason_codes", "below_threshold",
    "demote_recommended", "note"}}`` sorted by source_id.
    """
    by_source = {}
    for row in feedback_rows:
        verdict = row.get("verdict")
        if verdict not in POSITIVE_VERDICTS and verdict not in NEGATIVE_VERDICTS:
            continue
        sid = row.get("source_id")
        bucket = by_source.setdefault(sid, [])
        bucket.append(row)

    out = {}
    for sid in sorted(by_source, key=lambda s: (s is None, s)):
        rows = by_source[sid]
        rate = useful_rate_overall(rows)
        precision = rate["rate"]
        n = rate["total"]
        reason_codes = {
            item["reason_code"]: item["count"]
            for item in reason_code_distribution(rows)
        }
        below_threshold = precision is not None and precision < threshold
        if n < min_n:
            demote_recommended = False
            note = (
                f"sample too small (n={n}<{min_n}); not enough evidence to "
                "recommend demotion")
        elif below_threshold:
            demote_recommended = True
            note = (
                f"precision {precision:.0%}<{threshold:.0%} over n={n}; review "
                "reason codes then consider store-only/disable")
        else:
            demote_recommended = False
            note = f"precision at/above {threshold:.0%} threshold over n={n}"
        out[sid] = {
            "precision": precision,
            "n": n,
            "reason_codes": reason_codes,
            "below_threshold": below_threshold,
            "demote_recommended": demote_recommended,
            "note": note,
        }
    return out


# -- CLI: the monthly precision job (R9.3) -----------------------------------
#
#     python -m app.audit.precision --report
#
# NO new table, and no ingest lock. The report is a pure function of rows that
# are already durable and append-only (``feedback`` + ``audit``), so it is
# reproducible for any month at any time; a snapshot table would be a second
# copy of derivable numbers with nothing reading it. What the monthly job adds
# is the RECORD: cron appends this run's lines to the container log, dated, so
# precision per trigger / source / signal_scope is observable over time rather
# than only in the moment someone opens the Precision page. The job reads and
# prints; it never writes the store, so it takes no single-writer lock (R3.2) —
# ``ingest_lock()`` RAISES on a live lock, which would turn a read-only report
# into a spurious failure whenever a pipeline happened to be running. For the
# same reason the crontab does NOT wrap it in deploy/scheduled_run.sh: that
# guard exists to serialize WRITERS, and it exits 0 on contention, so wrapping a
# read-only monthly job would silently discard that month's record whenever a
# first-load or manual ingest happened to be in flight.

def prior_month_end(now=None):
    """Last instant of the UTC calendar month BEFORE ``now``.

    The scheduled job fires just after midnight-ish on the 1st, so ``now`` is a
    few hours into a month that has barely started. Every windowed metric in the
    report (today: ``spotcheck_coverage``) windows to the calendar month
    CONTAINING the ``now`` it is given, so asking it about the run's own instant
    would summarize ~16 hours of the new month and never once report the month
    that just ended — a permanently, always-false line in a permanent record.
    Handing it this instant instead makes the scheduled record summarize the
    completed month.

    This does NOT change ``spotcheck_coverage``'s semantics: the Precision page
    asks about the current month, which is right for a live view. It is only
    what the SCHEDULED invocation asks for.
    """
    now_dt = _now(now)
    first_of_month = now_dt.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_of_month - timedelta(microseconds=1)


def _fmt_rate(value):
    """A rate as a percent, or ``n/a`` when the denominator was empty.

    Never a bare number: every caller below prints this beside the ``n`` it was
    computed over, so the module's trust invariant survives into the log.
    """
    return "n/a" if value is None else f"{value:.1%}"


def format_report(report, as_of=None):
    """Render a ``app.ui.data.precision_report`` dict as the R9.3 record.

    One summary line (``precision: success …``, matching the other producers'
    one-line convention) followed by one line per dimension bucket for each of
    ``REPORT_DIMENSIONS``. Every rate is printed beside its ``n``. Returns the
    lines as a list so the shaping is testable without a store.

    ``as_of`` (an ISO-8601 string) is appended to the summary line when given,
    so a log record states the instant it was computed for — which, for the
    scheduled run, is the last instant of the month being reported and NOT the
    run's own wall clock. ``spotcheck_window`` names that month outright.
    The overall useful/auto rates are lifetime-cumulative, not monthly, so no
    line claims the whole record is scoped to one month.
    """
    useful = report["useful_overall"]
    auto = report["auto_overall"]
    spot = report["spotcheck"]
    g2 = report["g2"]
    # Sector and regulatory cards have no raw_event, so g2_status buckets their
    # NULL source_id together as though it were a source. That bucket is not a
    # source and must not be counted on either side of the headline: this store
    # is dominated by sector/regulatory cards, so counting it would report
    # sources as "assessed" when zero real sources were (R9.5).
    real_sources = [cell for sid, cell in g2.items() if sid is not None]
    demote = sum(1 for cell in real_sources if cell["demote_recommended"])
    summary = (
        f"precision: success useful={_fmt_rate(useful['rate'])} "
        f"n={useful['total']} auto={_fmt_rate(auto['accuracy'])} "
        f"n={auto['scored']} g1={report['g1']['state']} "
        f"g2_demote_recommended={demote}/{len(real_sources)} "
        f"spotcheck={spot['reviewed']}/{spot['target']} "
        f"spotcheck_window={spot['window']}"
    )
    if as_of is not None:
        summary += f" as_of={as_of}"
    lines = [summary]
    for dimension in REPORT_DIMENSIONS:
        useful_buckets = report["useful_by_dimension"][dimension]
        auto_buckets = report["auto_by_dimension"][dimension]
        keys = (set(useful_buckets) | set(auto_buckets)) - {"overall"}
        for key in sorted(keys, key=lambda k: (k is None, k)):
            u = useful_buckets.get(key)
            a = auto_buckets.get(key)
            lines.append(
                f"precision {dimension}={'(none)' if key is None else key} "
                f"useful={_fmt_rate(u['rate']) if u else 'n/a'} "
                f"n={u['total'] if u else 0} "
                f"auto={_fmt_rate(a['accuracy']) if a else 'n/a'} "
                f"n={a['scored'] if a else 0}")
    return lines


def main(argv=None, now=None):
    """Compute and emit the monthly precision report (R9.3). Returns 0.

    The report is computed as of the last instant of the PRIOR calendar month
    (``prior_month_end``), because the job is scheduled on the 1st: asking for
    the run's own instant would summarize a few hours of the month that just
    started and never once report the month that ended. ``now`` is injectable so
    a test can pin the real cron instant.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.audit.precision",
        description="Compute precision per trigger, source and signal_scope "
                    "over the stored feedback and audit verdicts, for the "
                    "calendar month that just ended, and print it (R9.3). "
                    "Writes nothing to the store.")
    parser.add_argument(
        "--report", action="store_true",
        help="compute the report and emit it to stdout (the monthly R9.3 job)")
    args = parser.parse_args(argv)
    if not args.report:
        parser.error("no action requested; pass --report")

    # Deferred import: app.ui.data imports THIS module, so a module-level
    # import would be a cycle. precision_report is the ONE place the four
    # row-reads and the R9.11 gate overlay are composed (R10.9) — reusing it
    # is what keeps this job and the Precision page from ever disagreeing.
    from app.ui import data

    as_of = prior_month_end(now)
    conn = get_connection()
    try:
        report = data.precision_report(conn, now=as_of)
    finally:
        conn.close()
    for line in format_report(report, as_of=as_of.isoformat()):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
