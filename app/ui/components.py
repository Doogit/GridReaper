"""Shared card rendering for the GridSignals UI (R8.1).

STUB — the API is fixed here in Phase A so B2 (Review Queue) and B3
(Account 360) can build against it; B1 implements the bodies during the
Signal Feed build. B2/B3 consume these read-only. Card HTML uses the classes
defined in app/ui/theme.py.

The card is driven by a data.signal_detail() dict (its 'signal', 'evidence',
and 'snapshots' keys). Both the feed and Account 360 render cards from that
same shape, so there is one card and one code path.
"""

# Score bands for the severity strip. B1 fills in the thresholds (base
# strengths are 4-5; a fresh account card ~4+, sector cards ~2.75, decay
# threshold 1.0) so the strip reads meaningfully against real scores.
SEVERITY_BANDS = ("critical", "high", "moderate", "low")


def severity_band(score):
    """Map a signal ``score`` to one of SEVERITY_BANDS for the severity strip
    (R8.1). B1 implements the thresholds."""
    raise NotImplementedError("B1: severity strip thresholds")


def render_signal_card(container, detail, legend, *, key, on_feedback=None):
    """Render one signal card (full R8.1 anatomy) into a Streamlit container.

    detail   a data.signal_detail() dict (signal row + evidence + snapshots).
    legend   data.badge_legend(), so badge labels/tooltips are never hardcoded.
    key      unique Streamlit widget-key prefix for this card's controls.
    on_feedback  callback(signal_id, verdict, reason_code, note) invoked when
                 the user submits useful/not_useful (reason-code select shown
                 on not_useful, R9.1/R9.2); the page wires this to
                 data.record_feedback. None disables the feedback controls
                 (e.g. read-only contexts).

    Anatomy (B1): severity strip, decay bar (score vs fresh base_strength x
    fits from the persisted score_* components), confidence indicator, evidence
    badge with legend tooltip, scope badge, coverage badge (account cards),
    product/play chips and gov-cloud caution line from snapshots, expandable
    evidence rows (evidence_text + locator + source link), outreach draft shown
    ONLY from snapshot.outreach_safe_text and only when
    signal.customer_facing_allowed, non-primary fact chips badged (R4.3) with
    no price_note text anywhere in the DOM.
    """
    raise NotImplementedError("B1: full card anatomy")


def render_feed_divider(container, label):
    """Render the labeled sector/regulatory divider between the account-scoped
    cards and the broader cards (R7.2), so sector cards never visually mix into
    account cards. B1 implements."""
    raise NotImplementedError("B1: scope divider")


def render_empty_state(container, message):
    """Render an honest empty-state panel (R6.6): a quiet surface reads as
    'low volume by design', never as broken. B1 implements."""
    raise NotImplementedError("B1: empty state")
