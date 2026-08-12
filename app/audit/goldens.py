"""Golden-set regression harness for the audit judge (R9.10).

A change to the judge prompt/schema/parser (PROMPT_VERSION / PARSER_VERSION in
``app.audit.schema``) or a model swap MUST clear the golden set before its
verdicts count toward gates. This module loads the reviewed goldens from the
``audit_goldens`` seed table, replays each fixture through a judge client, and
reports the pass rate against ``GOLDEN_PASS_THRESHOLD``.

A golden pins the *expected* verdict for one or more of the four objective
checks (R9.7); a golden passes only when every pinned check matches. The client
is duck-typed: any object exposing ``.judge(record) -> JudgeResult`` works, so
tests inject a canned client (no network, ever) and the CLI passes a real
``AuditClient``.

Provenance: ``seeds/audit_goldens.csv`` is generated-for-review
(``reviewed_by='generated-for-review'``) and awaits Kevin's review; it does not
alter the hand-verified seeds.
"""
import json
import sys

# Minimum golden pass rate a judge/prompt/model change must clear before its
# verdicts count toward gates (R9.10). A regression below this blocks.
GOLDEN_PASS_THRESHOLD = 0.90


def load_goldens(conn):
    """Load reviewed goldens from the audit_goldens table.

    Returns a list of dicts:
      {golden_id, record (parsed signal_fixture),
       expected (parsed expected_results dict), reviewed_by, reviewed_at}

    Raises ValueError with the golden_id if either JSON column is malformed."""
    rows = conn.execute(
        "SELECT golden_id, signal_fixture, expected_results, reviewed_by, "
        "reviewed_at FROM audit_goldens ORDER BY golden_id").fetchall()
    goldens = []
    for row in rows:
        gid = row["golden_id"]
        try:
            record = json.loads(row["signal_fixture"])
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"golden {gid!r}: signal_fixture is not valid JSON: {exc}")
        try:
            expected = json.loads(row["expected_results"])
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"golden {gid!r}: expected_results is not valid JSON: {exc}")
        if not isinstance(record, dict):
            raise ValueError(f"golden {gid!r}: signal_fixture is not an object")
        if not isinstance(expected, dict):
            raise ValueError(
                f"golden {gid!r}: expected_results is not an object")
        goldens.append({
            "golden_id": gid,
            "record": record,
            "expected": expected,
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
        })
    return goldens


def run_goldens(client, goldens):
    """Replay each golden through ``client.judge`` and compare pinned checks.

    A golden "passes" only if the judge returns status "ok" AND every pinned
    expected check equals the judge's verdict result for that check. A judge
    status other than "ok" (unavailable/over_budget/error) marks the golden
    errored (and not-passed). pass_rate is over the total goldens, so errored
    goldens count against it.

    Returns a report dict::

        {total, passed, failed:[...], errored:[...], pass_rate, threshold,
         meets_threshold, details:[...]}

    ``failed`` entries carry the per-check mismatches; ``details`` carries one
    record per golden for reporting."""
    total = len(goldens)
    passed = 0
    failed = []
    errored = []
    details = []

    for g in goldens:
        gid = g["golden_id"]
        result = client.judge(g["record"])
        if result.status != "ok":
            errored.append({"golden_id": gid, "status": result.status,
                            "error": result.error})
            details.append({"golden_id": gid, "outcome": "errored",
                            "status": result.status, "error": result.error})
            continue

        verdicts = result.verdicts or {}
        mismatches = []
        for check, want in g["expected"].items():
            got = (verdicts.get(check) or {}).get("result")
            if got != want:
                mismatches.append({"check": check, "expected": want,
                                   "got": got})
        if mismatches:
            failed.append({"golden_id": gid, "mismatches": mismatches})
            details.append({"golden_id": gid, "outcome": "failed",
                            "mismatches": mismatches})
        else:
            passed += 1
            details.append({"golden_id": gid, "outcome": "passed"})

    pass_rate = (passed / total) if total else 0.0
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "pass_rate": pass_rate,
        "threshold": GOLDEN_PASS_THRESHOLD,
        "meets_threshold": pass_rate >= GOLDEN_PASS_THRESHOLD,
        "details": details,
    }


def cli():
    """Run the golden set against a real judge and print the report.

    Exit-code contract:
      0  - no real judgments ran (no API key: the harness is gated behind key
           availability, mirroring R9.12), OR the run met the threshold.
      1  - real judgments ran and did NOT meet GOLDEN_PASS_THRESHOLD (a
           regression, made visible to callers/CI).
    """
    from app.audit.client import AuditClient
    from app.db.connection import get_connection

    client = AuditClient()
    if not client.available():
        print("Golden run skipped: no ANTHROPIC_API_KEY set. The golden "
              "harness needs an API key to replay fixtures through the judge "
              "(R9.12). Set ANTHROPIC_API_KEY and re-run.")
        return 0

    conn = get_connection()
    try:
        goldens = load_goldens(conn)
    finally:
        conn.close()

    report = run_goldens(client, goldens)
    print("=== Audit golden set (R9.10) ===")
    print(f"model: {client.model_id}")
    print(f"goldens: {report['total']}  passed: {report['passed']}  "
          f"failed: {len(report['failed'])}  errored: {len(report['errored'])}")
    print(f"pass_rate: {report['pass_rate']:.3f}  "
          f"threshold: {report['threshold']:.2f}  "
          f"{'MEETS' if report['meets_threshold'] else 'BLOCKED'}")
    for f in report["failed"]:
        for m in f["mismatches"]:
            print(f"  FAIL [{f['golden_id']}] {m['check']}: "
                  f"expected {m['expected']!r}, got {m['got']!r}")
    for e in report["errored"]:
        print(f"  ERROR [{e['golden_id']}] status={e['status']}: {e['error']}")

    return 0 if report["meets_threshold"] else 1


if __name__ == "__main__":
    sys.exit(cli())
