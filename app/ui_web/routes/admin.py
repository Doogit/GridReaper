"""Admin / config routes (R8.7; R3.2, R3.3, R7.4, R7.5, R7.6, R10.7).

The operator's tuning surface and the app's write path into seeded config
tables — the TestClient port of app/ui/pages/5_Admin.py. Sections: scoring
weights (R7.5), decay half-lives (R7.4), source registry (R8.7/R9.5), watchlist
entity manager (R8.7), license-fact staleness + editor (R10.7/R7.6), and a
config-audit provenance trail (R3.3).

Every write goes through data.config_write_conn() (the single-writer ingestion
lock, R3.2): a RuntimeError (an ingestion/scoring run holds the lock) becomes a
WARNING flash and writes nothing; a ValueError becomes an inline error flash and
writes nothing — never a silent or torn write. The flash is delivered by an
out-of-band (OOB) HTMX swap into #flash, and the affected section is re-rendered
from the DB so the page reflects the write without a full reload.

Destructive actions (entity reset/remove, source remove) are gated SERVER-SIDE:
the POST carries a ``confirm`` flag and the helper fires only when it is truthy.
A POST without it is a real no-op that flashes a "check the box" warning — not a
client-disabled button. The frozen-score contract is preserved by the data.py
helpers (only weight/half-life rescore active rows; entity/fact edits never move
an existing card). Price never reaches the DOM (license_fact_rows omits nothing
we render — price_note is shown only via the editable-column form field, exactly
as the Streamlit page did, and never on a card). UTC ISO-8601 (R10.2).

The UI stays a read-only reader over app/ui/data.py; the only writes are the
config helpers, each under the lock via deps.config_write.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.ui import data
from app.ui_web import render
from app.ui_web.deps import config_write, get_db, get_db_async
from app.ui_web.templating import templates

router = APIRouter()

STALE_FACT_WINDOW_DAYS = 180


# -- section context builders (read from DB each render) ---------------------

def _tuning_ctx(conn):
    """Scoring weights + decay half-lives, partitioned by whether they reach a
    live card (R8.7). data.tuning_usage() replays the scorer's own key
    selection over active signals, so "live" is measured, not hand-ordered: the
    day an account-scoped signal lands, its subsector/richness/coverage knobs
    promote themselves out of the inert tier with no code change."""
    usage = data.tuning_usage(conn)
    live, inert = [], []
    for r in conn.execute("SELECT weight_kind, key, weight FROM scoring_weights "
                          "ORDER BY weight_kind, key"):
        row = {"kind": r["weight_kind"], "key": r["key"],
               "label": (f"{render.weight_kind_label(r['weight_kind'])}"
                         f" · {r['key']}"),
               "field": f"w:{r['weight_kind']}:{r['key']}",
               "value": f"{float(r['weight']):.2f}", "step": "0.05",
               "min": "0", "max": ""}
        (live if (r["weight_kind"], r["key"]) in usage["weights"]
         else inert).append(row)
    for r in conn.execute("SELECT trigger_id, name, decay_half_life_days "
                          "FROM triggers ORDER BY trigger_id"):
        row = {"kind": "half_life", "key": r["trigger_id"],
               "label": f"{r['name']} — half-life days",
               "field": f"h:{r['trigger_id']}",
               "value": str(r["decay_half_life_days"]), "step": "1",
               "min": "1", "max": "3650"}
        (live if r["trigger_id"] in usage["triggers"] else inert).append(row)
    return {"tuning_live": live, "tuning_inert": inert,
            "tuning_live_n": len(live), "tuning_inert_n": len(inert),
            "active_card_n": conn.execute(
                "SELECT COUNT(*) FROM signals WHERE status = 'active'"
            ).fetchone()[0]}


def _sources_ctx(conn):
    rows = data.source_policy_rows(conn)
    sources = []
    for r in rows:
        d = dict(r)
        removable = (r.get("origin") == "operator"
                     and r.get("reference_count", 0) == 0)
        refs = {}
        if r.get("origin") == "operator" and r.get("reference_count", 0):
            refs = data.source_reference_breakdown(conn, r["source_id"])
        d["removable"] = removable
        d["is_operator"] = r.get("origin") == "operator"
        d["ref_breakdown"] = refs
        d["ref_total"] = sum(refs.values())
        sources.append(d)
    return {"sources": sources}


def _entities_ctx(conn, selected=""):
    rows = data.entity_editor_rows(conn)
    selected = _resolve_entity(rows, selected)
    detail = _entity_detail_ctx(conn, rows, selected) if rows else {}
    ctx = {"entities": rows, "selected_entity": selected}
    ctx.update(detail)
    return ctx


def _resolve_entity(rows, selected):
    ids = {r["entity_id"] for r in rows}
    if selected in ids:
        return selected
    return rows[0]["entity_id"] if rows else ""


def _entity_detail_ctx(conn, rows, entity_id):
    row = next((r for r in rows if r["entity_id"] == entity_id), None)
    if row is None:
        return {"entity": None}
    terms = data.entity_alias_rows(conn, entity_id)
    ref_breakdown = {}
    if row["origin"] != "seed":
        ref_breakdown = data.entity_reference_breakdown(conn, entity_id)
    return {
        "entity": row,
        "entity_active_label": "Active" if row["active"] else "Disabled",
        "aliases": terms["aliases"],
        "collision_terms": terms["collision_terms"],
        "entity_ref_breakdown": ref_breakdown,
        "entity_ref_total": sum(ref_breakdown.values()),
    }


def _facts_ctx(conn, selected=""):
    stale = data.stale_facts(conn, days=STALE_FACT_WINDOW_DAYS)
    rows = data.license_fact_rows(conn)
    selected = _resolve_fact(rows, selected)
    detail = _fact_detail_ctx(conn, rows, selected) if rows else {}
    ctx = {
        "stale_count": len(stale),
        "stale_window_days": STALE_FACT_WINDOW_DAYS,
        "stale_facts": render.staleness_view(stale),
        "facts": rows,
        "selected_fact": selected,
        "fact_segments": data.FACT_SEGMENTS,
        "fact_source_quality": data.FACT_SOURCE_QUALITY,
    }
    ctx.update(detail)
    return ctx


def _resolve_fact(rows, selected):
    ids = {r["fact_id"] for r in rows}
    if selected in ids:
        return selected
    return rows[0]["fact_id"] if rows else ""


def _fact_detail_ctx(conn, rows, fact_id):
    row = next((r for r in rows if r["fact_id"] == fact_id), None)
    if row is None:
        return {"fact": None}
    fields = []
    for col in data.EDITABLE_FACT_COLS:
        fields.append({"name": col, "value": row.get(col) or "",
                       "is_quality": col == "source_quality"})
    return {
        "fact": row,
        "fact_label": (row.get("product_name") or row.get("product_id") or "—"),
        "fact_citations": data.fact_snapshot_citations(conn, fact_id),
        "fact_fields": fields,
    }


def _audit_ctx(conn):
    return {"audit": render.audit_view(data.config_audit_tail(conn, limit=25))}


def _page_context(conn, reason=""):
    ctx = {"reason": reason, "stale_window_days": STALE_FACT_WINDOW_DAYS,
           "nav_active": "admin", "flash": None}
    ctx.update(_tuning_ctx(conn))
    ctx.update(_sources_ctx(conn))
    ctx.update(_entities_ctx(conn))
    ctx.update(_facts_ctx(conn))
    ctx.update(_audit_ctx(conn))
    return ctx


# -- write plumbing (single-writer lock; flash via OOB swap) -----------------

def _run_write(fn, *args, **kwargs):
    """Run a config-write helper under the single-writer lock (R3.2) and return
    a flash dict. RuntimeError (lock held) -> a warning; ValueError -> an inline
    error; success -> the helper's result message. NEVER writes on either error
    path (the helper itself is transactional). Returns
    {'level', 'text'} — the OOB flash the caller swaps into #flash."""
    try:
        with config_write() as wconn:
            result = fn(wconn, *args, **kwargs)
    except RuntimeError:
        return {"level": "warning",
                "text": ("Ingestion in progress — the single-writer lock is "
                         "held. Try again once the run finishes.")}
    except ValueError as exc:
        return {"level": "error", "text": str(exc)}
    return {"level": render.result_level(fn.__name__, result),
            "text": render.result_message(fn.__name__, result)}


def _section(request, name, ctx, flash):
    """Render one section partial with the OOB flash embedded. The partial swaps
    into its section container; the flash rides along as an hx-swap-oob div."""
    ctx = dict(ctx)
    ctx["flash"] = flash
    ctx["partial"] = True
    return templates.TemplateResponse(request=request, name=name, context=ctx)


# -- page + section GETs -----------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
def admin(request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse(
        request=request, name="admin.html", context=_page_context(conn))


# -- scoring weights ---------------------------------------------------------

@router.post("/admin/tuning", response_class=HTMLResponse)
async def save_tuning(request: Request, conn=Depends(get_db_async)):
    """Save a whole tier of tuning knobs in one write (R7.4/R7.5).

    Field names carry their own identity ("w:{weight_kind}:{key}",
    "h:{trigger_id}") so one form can post any mix of weights and half-lives.
    The reason rides in the same submission, so provenance is attached to the
    save the operator is actually looking at. Unknown or malformed field names
    are ignored rather than trusted — the form is generated from the DB, so a
    name that does not parse did not come from this page."""
    form = await request.form()
    weight_edits, half_life_edits = [], []
    for name, value in form.multi_items():
        if name.startswith("w:"):
            parts = name.split(":", 2)
            if len(parts) == 3:
                weight_edits.append((parts[1], parts[2], value))
        elif name.startswith("h:"):
            half_life_edits.append((name[2:], value))
    flash = _run_write(data.update_tuning, weight_edits, half_life_edits,
                       reason=form.get("reason", ""))
    return _section(request, "_admin_tuning.html", _tuning_ctx(conn), flash)


# -- source registry ---------------------------------------------------------

@router.post("/admin/source/enabled", response_class=HTMLResponse)
def save_source_enabled(request: Request, source_id: str = Form(...),
                        enabled: str = Form(""), reason: str = Form(""),
                        conn=Depends(get_db)):
    flash = _run_write(data.set_source_enabled, source_id,
                       enabled == "true", reason=reason)
    return _section(request, "_admin_sources.html", _sources_ctx(conn), flash)


@router.post("/admin/source/add", response_class=HTMLResponse)
def add_source(request: Request, source_id: str = Form(""),
               name: str = Form(""), access_method: str = Form(""),
               evidence_rank: str = Form(""), reason: str = Form(""),
               conn=Depends(get_db)):
    flash = _run_write(data.add_source, source_id, name,
                       access_method=access_method.strip(),
                       evidence_rank=evidence_rank.strip(), reason=reason)
    return _section(request, "_admin_sources.html", _sources_ctx(conn), flash)


@router.post("/admin/source/remove", response_class=HTMLResponse)
def remove_source(request: Request, source_id: str = Form(...),
                  confirm: str = Form(""), reason: str = Form(""),
                  conn=Depends(get_db)):
    # Server-side confirm gate: with no confirm flag this is a real no-op.
    if confirm != "true":
        flash = {"level": "warning",
                 "text": ("Check the box to confirm — Remove permanently "
                          "deletes this operator-added source.")}
    else:
        flash = _run_write(data.remove_source, source_id, reason=reason)
    return _section(request, "_admin_sources.html", _sources_ctx(conn), flash)


# -- watchlist entities ------------------------------------------------------

@router.post("/admin/entity/add", response_class=HTMLResponse)
def add_entity(request: Request, entity_id: str = Form(""),
               name: str = Form(""), subsector: str = Form(""),
               gov_cloud_likelihood: str = Form(""), reason: str = Form(""),
               conn=Depends(get_db)):
    fields = {}
    if subsector.strip():
        fields["subsector"] = subsector.strip()
    if gov_cloud_likelihood.strip():
        fields["gov_cloud_likelihood"] = gov_cloud_likelihood.strip()
    flash = _run_write(data.add_watchlist_entity, entity_id, name,
                       fields=fields, reason=reason)
    selected = entity_id.strip() if flash["level"] == "success" else ""
    return _section(request, "_admin_entities.html",
                    _entities_ctx(conn, selected), flash)


@router.post("/admin/entity/active", response_class=HTMLResponse)
def save_entity_active(request: Request, entity_id: str = Form(...),
                       active: str = Form(""), reason: str = Form(""),
                       conn=Depends(get_db)):
    flash = _run_write(data.set_entity_active, entity_id, active == "true",
                       reason=reason)
    return _section(request, "_admin_entities.html",
                    _entities_ctx(conn, entity_id), flash)


@router.post("/admin/entity/field", response_class=HTMLResponse)
def save_entity_field(request: Request, entity_id: str = Form(...),
                      field: str = Form(...), value: str = Form(""),
                      reason: str = Form(""), conn=Depends(get_db)):
    flash = _run_write(data.update_watchlist_entity, entity_id, field, value,
                       reason=reason)
    return _section(request, "_admin_entities.html",
                    _entities_ctx(conn, entity_id), flash)


@router.post("/admin/entity/alias/add", response_class=HTMLResponse)
def add_alias(request: Request, entity_id: str = Form(...),
              alias: str = Form(""), reason: str = Form(""),
              conn=Depends(get_db)):
    flash = _run_write(data.add_alias, entity_id, alias, reason=reason)
    return _section(request, "_admin_entities.html",
                    _entities_ctx(conn, entity_id), flash)


@router.post("/admin/entity/alias/remove", response_class=HTMLResponse)
def remove_alias(request: Request, entity_id: str = Form(...),
                 alias: str = Form(...), reason: str = Form(""),
                 conn=Depends(get_db)):
    flash = _run_write(data.remove_alias, entity_id, alias, reason=reason)
    return _section(request, "_admin_entities.html",
                    _entities_ctx(conn, entity_id), flash)


@router.post("/admin/entity/term/add", response_class=HTMLResponse)
def add_term(request: Request, entity_id: str = Form(...),
             term: str = Form(""), reason: str = Form(""),
             conn=Depends(get_db)):
    flash = _run_write(data.add_collision_term, entity_id, term, reason=reason)
    return _section(request, "_admin_entities.html",
                    _entities_ctx(conn, entity_id), flash)


@router.post("/admin/entity/term/remove", response_class=HTMLResponse)
def remove_term(request: Request, entity_id: str = Form(...),
                term: str = Form(...), reason: str = Form(""),
                conn=Depends(get_db)):
    flash = _run_write(data.remove_collision_term, entity_id, term,
                       reason=reason)
    return _section(request, "_admin_entities.html",
                    _entities_ctx(conn, entity_id), flash)


@router.post("/admin/entity/reset", response_class=HTMLResponse)
def reset_entity(request: Request, entity_id: str = Form(...),
                 confirm: str = Form(""), reason: str = Form(""),
                 conn=Depends(get_db)):
    if confirm != "true":
        flash = {"level": "warning",
                 "text": ("Check the box to confirm — Reset reverts your edits "
                          "to the seed values and re-enables the entity.")}
    else:
        flash = _run_write(data.reset_entity_to_seed, entity_id, reason=reason)
    return _section(request, "_admin_entities.html",
                    _entities_ctx(conn, entity_id), flash)


@router.post("/admin/entity/remove", response_class=HTMLResponse)
def remove_entity(request: Request, entity_id: str = Form(...),
                  confirm: str = Form(""), reason: str = Form(""),
                  conn=Depends(get_db)):
    if confirm != "true":
        flash = {"level": "warning",
                 "text": ("Check the box to confirm — Remove permanently "
                          "deletes this operator-added entity.")}
        selected = entity_id
    else:
        flash = _run_write(data.remove_operator_entity, entity_id, reason=reason)
        # A successful remove drops the entity; fall back to the default pick.
        selected = "" if flash["level"] == "success" else entity_id
    return _section(request, "_admin_entities.html",
                    _entities_ctx(conn, selected), flash)


# -- license facts -----------------------------------------------------------

@router.post("/admin/fact/add", response_class=HTMLResponse)
def add_fact(request: Request, fact_id: str = Form(""),
             product_id: str = Form(""), segment: str = Form(""),
             price_note: str = Form(""), source_quality: str = Form(""),
             source_url: str = Form(""), verified_date: str = Form(""),
             reason: str = Form(""), conn=Depends(get_db)):
    fields = {}
    if price_note.strip():
        fields["price_note"] = price_note.strip()
    if source_quality:
        fields["source_quality"] = source_quality
    if source_url.strip():
        fields["source_url"] = source_url.strip()
    if verified_date.strip():
        fields["verified_date"] = verified_date.strip()
    if segment:
        fields["segment"] = segment
    flash = _run_write(data.add_license_fact, fact_id, product_id,
                       fields=fields, reason=reason)
    selected = fact_id.strip() if flash["level"] == "success" else ""
    return _section(request, "_admin_facts.html", _facts_ctx(conn, selected),
                    flash)


@router.post("/admin/fact/edit", response_class=HTMLResponse)
def edit_fact(request: Request, fact_id: str = Form(...),
              field: str = Form(...), value: str = Form(""),
              reason: str = Form(""), conn=Depends(get_db)):
    flash = _run_write(data.update_license_fact, fact_id, field, value,
                       reason=reason)
    return _section(request, "_admin_facts.html", _facts_ctx(conn, fact_id),
                    flash)


# -- selector swaps (read-only; pick a different entity / fact) --------------

@router.get("/admin/entities", response_class=HTMLResponse)
def entities_section(request: Request, entity_id: str = "",
                     conn=Depends(get_db)):
    return _section(request, "_admin_entities.html",
                    _entities_ctx(conn, entity_id), None)


@router.get("/admin/facts", response_class=HTMLResponse)
def facts_section(request: Request, fact_id: str = "", conn=Depends(get_db)):
    return _section(request, "_admin_facts.html",
                    _facts_ctx(conn, fact_id), None)
