"""Incident evidence-tier editor routes (R8.7; R10.5, R7.12, R3.2, R3.3).

The operator confirms/re-tiers an incident card between the R10.5 tiers
(confirmed / corroborated / unconfirmed_early_warning). This is the UI's first
write that mutates a ``signals`` row's state, so it goes through the same
single-writer lock as the config editors: ``data.config_write_conn()`` acquires
the ingestion lock (R3.2); a RuntimeError (an ingestion/scoring run holds it)
becomes a WARNING and writes nothing, a ValueError becomes an inline error and
writes nothing — never a torn write. Every applied re-tier appends one immutable
``incident_tier_edits`` row (R3.3) and re-derives ``customer_facing_allowed``
from the new tier (R7.12) via the classifier's own rule.

Two routes, mirroring the feed's feedback reveal/submit pair:
  GET  /incident/tier   reveal the re-tier panel (form + history) into the card
  POST /incident/tier   apply the re-tier, re-render the whole card so the
                        verification-first warning, outreach state, and accent
                        follow the new tier (card_view derives all three from
                        incident_evidence_level + customer_facing_allowed).

The read seam (data.signal_detail / render.card_view) is unchanged; only the
lock-guarded write helper mutates the signal.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.ui import data
from app.ui_web import render
from app.ui_web.deps import config_write, get_db
from app.ui_web.templating import templates

router = APIRouter()


def _run_retier(signal_id, new_level, reason=""):
    """Apply one re-tier under the single-writer lock (R3.2) and return a flash
    dict {level, text}. RuntimeError (lock held) -> warning; ValueError
    (unknown tier / not an incident) -> inline error; success -> the result
    message. NEVER writes on either error path (the helper is transactional)."""
    try:
        with config_write() as wconn:
            result = data.retier_incident(wconn, signal_id, new_level,
                                          reason=reason)
    except RuntimeError:
        return {"level": "warning",
                "text": ("Ingestion in progress — the single-writer lock is "
                         "held. Try again once the run finishes.")}
    except ValueError as exc:
        return {"level": "error", "text": str(exc)}
    return {"level": render.result_level("retier_incident", result),
            "text": render.result_message("retier_incident", result)}


def _panel_ctx(conn, signal_id, incident_level, flash=None):
    """Context for the re-tier panel partial (form + history)."""
    return {"signal_id": signal_id, "incident_level": incident_level,
            "tiers": list(data.INCIDENT_TIERS),
            "history": data.incident_tier_history(conn, signal_id),
            "flash": flash}


@router.get("/incident/tier", response_class=HTMLResponse)
def tier_panel(request: Request, signal_id: str, conn=Depends(get_db)):
    """Reveal the re-tier panel for one incident card (swapped into its
    tier container). A non-incident / unknown signal reveals nothing."""
    detail = data.signal_detail(conn, signal_id)
    if detail is None or detail["signal"]["incident_evidence_level"] is None:
        return HTMLResponse("")
    ctx = _panel_ctx(conn, signal_id,
                     detail["signal"]["incident_evidence_level"])
    return templates.TemplateResponse(
        request=request, name="_incident_tier.html", context=ctx)


@router.post("/incident/tier", response_class=HTMLResponse)
def retier(request: Request, signal_id: str = Form(...),
           new_level: str = Form(...), reason: str = Form(""),
           conn=Depends(get_db)):
    """Apply a re-tier. On success/no-op, re-render the WHOLE card from the (now
    committed) DB state with the panel kept open showing the flash + refreshed
    history, so the verification-first warning / outreach / accent follow the
    new tier. On error/warning (lock held, or a defensive reject) the write did
    nothing: HX-Retarget the swap to the tier container and return just the
    panel, so the card the operator was reading is preserved."""
    flash = _run_retier(signal_id, new_level, reason=reason)
    detail = data.signal_detail(conn, signal_id)
    if detail is None:
        return HTMLResponse(
            "<div class=\"gs-flash gs-flash-error\" role=\"status\">Signal not "
            "found.</div>")
    incident_level = detail["signal"]["incident_evidence_level"]

    if flash["level"] in ("error", "warning"):
        resp = templates.TemplateResponse(
            request=request, name="_incident_tier.html",
            context=_panel_ctx(conn, signal_id, incident_level, flash=flash))
        # keep the card intact — the write did nothing (R3.2 busy / defensive).
        resp.headers["HX-Retarget"] = f"#{render.tier_dom_id(signal_id)}"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp

    card = render.card_view(detail, data.badge_legend(conn))
    card["tier_open"] = True
    card["tier_flash"] = flash
    card["tier_history"] = data.incident_tier_history(conn, signal_id)
    card["tiers"] = list(data.INCIDENT_TIERS)
    return templates.TemplateResponse(
        request=request, name="_card.html", context={"c": card})
