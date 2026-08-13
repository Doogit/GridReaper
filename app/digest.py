"""Daily HTML digest generator (R8.8, R4.1, R7.12, R6.6, R10.2).

The last step of the packaged pipeline (after ``app.scoring && app.plays`` — see
KTD2): a standalone ``python -m app.digest`` CLI, NOT wired into the per-source
ingestion runner. It reads the freshest scored cards through the existing
read-only data layer (``app/ui/data.py``) and the same pure card shaper the web
UI uses (``render.card_view``), renders one self-contained HTML document
(``digest.html``, no request object, no server, no CDN), and writes it to a
dated file on disk so a human can send it (no SMTP here).

Trust invariants carried into the digest (identical to the live feed):
  * Outreach opener text appears ONLY when a card is ``customer_facing_allowed``
    (R7.12); ``card_view`` gates it, this module never re-adds it.
  * Every card keeps its evidence + source link (R4.1).
  * An empty / low-volume store still produces a VALID digest with honest
    "low-volume by design" copy (R6.6) — the file is always written.

Timestamps are UTC ISO-8601 (R10.2); the generation time is stamped in the
header and the footer.

Layout decision (Open Question #1 — deferred to Kevin): the digest leads with the
top account-scoped signals by score (the most likely outreach targets), then a
labeled divider, then Sector and Regulatory sections. Path/retention default:
``<db-dir>/digests/digest-YYYY-MM-DD.html`` (UTC date), plus a stable
``digest-latest.html`` copy so a server / link can always resolve "the latest".
Retention prunes dated files to the newest ``KEEP_DIGESTS`` writes.
"""
import glob
import os
import shutil
import sys
from datetime import datetime, timezone

from app.db.connection import DEFAULT_DB_PATH, get_connection
from app.ui import data
from app.ui_web import render
from app.ui_web.templating import templates

# How many account-scoped cards lead the digest. A digest is a curated top slice,
# not the whole feed; the rest stays in the app. (Open Question #1: lead count is
# a UX call for Kevin — this is a sensible default, easy to change.)
TOP_ACCOUNT_CARDS = 8

# Retention: keep the newest KEEP_DIGESTS dated files; older ones are pruned on
# each write so data/digests/ does not grow unbounded (Open Question #1 — the
# digest is a point-in-time artifact, not an archive). digest-latest.html is
# never pruned (it does not match the digest-2* glob).
KEEP_DIGESTS = 30

# Regulatory-calendar is its own section; the remaining sector-group scopes
# (sector, subsector) form the Sector section.
_REGULATORY_SCOPES = ("regulatory_calendar",)


def _digest_dir(db_path):
    """Directory digests are written to: a ``digests/`` folder beside the DB, so
    a throwaway test DB writes its digest next to itself, never into the repo."""
    parent = os.path.dirname(db_path) or "."
    return os.path.join(parent, "digests")


def _account_rows_by_score(conn, limit):
    """Top active account-scoped rows by score, with newest as the tie-breaker."""
    scopes = data.SCOPE_GROUPS["account"]
    placeholders = ",".join("?" * len(scopes))
    sql = (
        f"SELECT {data._SIGNAL_COLUMNS} {data._SIGNAL_FROM} "
        f"WHERE s.signal_scope IN ({placeholders}) AND s.status = 'active' "
        "ORDER BY s.score IS NULL, s.score DESC, s.event_date DESC, "
        "s.signal_id DESC LIMIT ?"
    )
    return conn.execute(sql, [*scopes, limit]).fetchall()


def _cards(conn, rows, legend):
    """Shape signal rows into card_view dicts, dropping any that no longer
    resolve (defensive — feed_page and signal_detail read the same table)."""
    cards = []
    for row in rows:
        detail = data.signal_detail(conn, row["signal_id"])
        if detail is not None:
            cards.append(render.card_view(detail, legend))
    return cards


def build_context(conn, now=None):
    """Assemble the digest template context from the read layer (R8.8).

    Top account cards (highest score first, capped at TOP_ACCOUNT_CARDS) + a
    Sector section + a Regulatory section, each shaped by ``render.card_view`` so
    they match the live feed. ``has_content`` is False when nothing active
    surfaces, which drives the honest empty-state copy (R6.6). ``now`` is
    injectable for deterministic tests; the stamp is UTC ISO-8601 (R10.2).
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    legend = data.badge_legend(conn)

    # Account-scoped, active, highest score first. feed_page orders by date; a
    # digest leads with the best leads, so query score-first directly.
    account_rows = _account_rows_by_score(conn, TOP_ACCOUNT_CARDS)

    sector_rows = data.feed_page(conn, scope_group="sector", limit=1000)
    reg_rows = [r for r in sector_rows if r["signal_scope"] in _REGULATORY_SCOPES]
    sec_rows = [r for r in sector_rows if r["signal_scope"] not in _REGULATORY_SCOPES]

    account_cards = _cards(conn, account_rows, legend)
    sector_cards = _cards(conn, sec_rows, legend)
    regulatory_cards = _cards(conn, reg_rows, legend)

    return {
        "generated_at": now.isoformat(),
        "generated_date": now.date().isoformat(),
        "account_cards": account_cards,
        "sector_cards": sector_cards,
        "regulatory_cards": regulatory_cards,
        "has_content": bool(account_cards or sector_cards or regulatory_cards),
    }


def render_html(ctx):
    """Render the standalone digest document (no request object, R8.8/U3)."""
    return templates.env.get_template("digest.html").render(ctx)


def write_digest(html, digest_dir, generated_date):
    """Write the digest to ``digest-<date>.html`` and refresh the stable
    ``digest-latest.html`` pointer. Creates the directory if missing. Returns the
    dated file path (the primary artifact)."""
    os.makedirs(digest_dir, exist_ok=True)
    dated = os.path.join(digest_dir, f"digest-{generated_date}.html")
    with open(dated, "w", encoding="utf-8") as fh:
        fh.write(html)
    # A predictable "latest" filename so a link / route can always find the newest
    # without globbing (the route reads the newest dated file; this is a fallback).
    shutil.copyfile(dated, os.path.join(digest_dir, "digest-latest.html"))
    _prune_old_digests(digest_dir)
    return dated


def _prune_old_digests(digest_dir, keep=KEEP_DIGESTS):
    """Delete all but the newest ``keep`` dated digests (R8.8 retention). Dated
    files (``digest-YYYY-MM-DD.html``) sort chronologically by name; the stable
    ``digest-latest.html`` alias is left untouched (it does not match the glob)."""
    dated = sorted(glob.glob(os.path.join(digest_dir, "digest-2*.html")))
    for stale in dated[:-keep] if keep > 0 else []:
        os.remove(stale)


def generate(db_path=None, digest_dir=None, now=None):
    """Build + render + write one digest; return the written file path (R8.8)."""
    resolved_db = db_path or os.environ.get("GRIDSIGNALS_DB") or DEFAULT_DB_PATH
    conn = get_connection(resolved_db)
    try:
        ctx = build_context(conn, now=now)
    finally:
        conn.close()
    html = render_html(ctx)
    out_dir = digest_dir or _digest_dir(resolved_db)
    return write_digest(html, out_dir, ctx["generated_date"])


def main(argv=None):
    """CLI entrypoint (R8.8). Wrap-and-log: as the pipeline's last step, a digest
    failure must not fail the whole build (KTD2) — report and exit non-zero only
    on error, but the earlier steps already committed."""
    try:
        path = generate()
    except Exception as exc:  # noqa: BLE001 — last-step wrap-and-log (KTD2)
        print(f"digest: FAILED to generate — {exc}", file=sys.stderr)
        return 1
    print(f"digest: wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
