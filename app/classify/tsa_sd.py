"""TSA Security Directive pipeline/LNG relevance gate (R8; U7 of the
combo-engine plan, docs/plans/2026-08-18-001-feat-combo-engine-account-signal-
expansion-plan.md).

Extends ``app/classify/regulatory.py``'s existing ``tsa_security_directive``
branch of ``classify_federal_register`` with the one precision gate the
merged probe (``app/spikes/tsa_sd_probe.py``, PR #106) proved necessary:
TSA's Federal Register agency filter admits documents on aviation, rail, and
general "surface transportation security" alongside pipeline/LNG security
directives, but ``app/obligations.py``'s ``APPLICABILITY`` maps EVERY
``tsa_security_directive`` signal, unconditionally, to the ``lng``/
``midstream`` subsectors. Without this gate, a rail-only or aviation-only
TSA security directive would derive a phantom pipeline/LNG compliance
obligation - a subsector-mismatch fabrication (R4.1) the classifier's
existing ``TSA_TERMS`` list (which itself includes "rail security" and
"surface transportation security") does nothing to prevent.

PIPELINE_LNG_TERMS mirrors ``app/spikes/tsa_sd_probe.py``'s relevance
heuristic verbatim (same three terms) rather than reinventing it - that
probe measured this exact heuristic against the real, live corpus before
this classifier gate was written. The constant is duplicated here rather
than imported from ``app/spikes/`` because probes are measurement-only,
disposable tooling (KTD10 of the combo-engine plan) and must never become a
runtime dependency of production classification code.

This module holds no ``SOURCES``/``CLASSIFIER_ID``/``cli()`` of its own - it
is not a second classifier competing with ``app/classify/regulatory.py`` for
the same trigger over the same raw_events (which would create duplicate-
classifier-ownership risk for the same deterministic signal_id). It is a
small, focused relevance check that ``classify_federal_register`` calls
inline, same as any other accept-rule term list in that module.
"""

PIPELINE_LNG_TERMS = ("pipeline", "lng", "liquefied natural gas")


def is_pipeline_lng_relevant(text):
    """True when TSA-agency-matched text (title + abstract, already
    lowercased by the caller) also names a pipeline/LNG term.

    ``app/classify/regulatory.py`` already matches the TSA agency reliably
    via ``_agency_slugs()``/``TSA_SLUG`` - this function does not re-derive
    agency membership (unlike the probe's ``is_tsa_sd_relevant``, which had
    no classifier-side agency match to build on and re-scanned
    ``agency_names`` itself). This function answers only the question the
    original ``TSA_TERMS`` rule left open: is the matched document actually
    about a pipeline or LNG facility, not rail, aviation, or a general
    surface-transportation topic.
    """
    return any(term in text for term in PIPELINE_LNG_TERMS)
