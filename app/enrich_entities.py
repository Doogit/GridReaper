"""Entity identifier enrichment (R4.2, R6.5).

Populates lei / wikidata_qid for watchlist entities plus GLEIF parent/child
relationships, writing generated seed CSVs for review:

  seeds/entity_identifiers.csv    entity_id, lei, wikidata_qid, method, verified_at
  seeds/entity_relationships.csv  parent_entity_id, child_entity_id,
                                  relationship_type, source, verified_at

Deterministic first: Wikidata is queried by CIK (P5531), returning QID and
LEI (P1278) in one batch; a second batch maps GLEIF-found LEIs back to QIDs.
GLEIF fulltext search is fallback only for entities still missing an LEI and
a candidate is accepted only on exact normalized-name match against the
entity name or a curated alias (fuzzy identifier writes are how wrong LEIs
happen). All access is read-only GET (R3.4). Re-run annually per R4.2:

  python -m app.enrich_entities
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from app.db.connection import get_connection
from app.resolve import normalize

USER_AGENT = "GridSignals/0.1 (+https://github.com/Doogit/GridSignals)"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
GLEIF_API = "https://api.gleif.org/api/v1"
GLEIF_SLEEP_S = 0.7   # stay well under GLEIF's 60 req/min

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
IDENTIFIERS_CSV = os.path.join(REPO_ROOT, "seeds", "entity_identifiers.csv")
RELATIONSHIPS_CSV = os.path.join(REPO_ROOT, "seeds", "entity_relationships.csv")


def _get_json(url, params=None, retries=2):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


# -- Wikidata ----------------------------------------------------------------

def _sparql(query):
    return _get_json(WIKIDATA_SPARQL, {"query": query, "format": "json"})


def wikidata_by_cik(ciks):
    """One batch query: CIK (P5531) -> (QID, LEI via P1278 if present).
    Wikidata stores CIKs zero-padded to 10 digits; results are keyed by the
    padded form."""
    values = " ".join(f'"{str(c).strip().zfill(10)}"' for c in ciks)
    q = ("SELECT ?cik ?item ?lei WHERE { ?item wdt:P5531 ?cik . "
         f"VALUES ?cik {{ {values} }} "
         "OPTIONAL { ?item wdt:P1278 ?lei } }")
    out = {}
    data = _sparql(q)
    for b in data["results"]["bindings"]:
        cik = b["cik"]["value"]
        qid = b["item"]["value"].rsplit("/", 1)[-1]
        lei = b.get("lei", {}).get("value", "")
        # keep first hit per CIK; extra hits are Wikidata dupes to hand-check
        out.setdefault(cik, (qid, lei))
    return out


def wikidata_by_lei(leis):
    """Second batch: LEI (P1278) -> QID, for entities GLEIF found."""
    if not leis:
        return {}
    values = " ".join(f'"{l}"' for l in leis)
    q = ("SELECT ?lei ?item WHERE { ?item wdt:P1278 ?lei . "
         f"VALUES ?lei {{ {values} }} }}")
    data = _sparql(q)
    return {b["lei"]["value"]: b["item"]["value"].rsplit("/", 1)[-1]
            for b in data["results"]["bindings"]}


# -- GLEIF -------------------------------------------------------------------

def gleif_candidates(name):
    """Fulltext search; returns [(lei, [names...], country), ...]."""
    data = _get_json(f"{GLEIF_API}/lei-records", {
        "filter[fulltext]": name, "page[size]": "10"})
    out = []
    for rec in (data or {}).get("data", []):
        ent = rec["attributes"]["entity"]
        names = [ent["legalName"]["name"]]
        names += [n["name"] for n in ent.get("otherNames", [])]
        country = (ent.get("legalAddress") or {}).get("country", "")
        out.append((rec["id"], names, country))
    return out


def accept_gleif(positive_terms, candidates):
    """Accept a GLEIF candidate only on exact normalized-name match against
    the entity's name/aliases, US-registered. Anything fuzzier is a wrong-LEI
    risk and is left for hand curation."""
    for lei, names, country in candidates:
        if country != "US":
            continue
        if any(normalize(n) in positive_terms for n in names):
            return lei
    return None


def gleif_direct_parent(lei):
    data = _get_json(f"{GLEIF_API}/lei-records/{lei}/direct-parent")
    if not data:
        return None
    return (data.get("data") or {}).get("id")


# -- main --------------------------------------------------------------------

def run():
    conn = get_connection()
    entities = conn.execute(
        "SELECT entity_id, name, cik, lei, wikidata_qid "
        "FROM watchlist_entities ORDER BY entity_id").fetchall()
    alias_rows = conn.execute(
        "SELECT entity_id, alias FROM entity_aliases").fetchall()
    conn.close()
    terms = {}   # entity_id -> set of normalized positive terms
    for e in entities:
        terms.setdefault(e["entity_id"], set()).add(normalize(e["name"]))
    for r in alias_rows:
        terms.setdefault(r["entity_id"], set()).add(normalize(r["alias"]))

    today = datetime.now(timezone.utc).date().isoformat()
    results = {}   # entity_id -> {lei, wikidata_qid, method}

    # 1) Wikidata by CIK (deterministic, one batch)
    with_cik = [e for e in entities if (e["cik"] or "").strip()]
    ciks = [e["cik"].strip() for e in with_cik]
    print(f"Wikidata: querying {len(ciks)} CIKs in one batch...")
    by_cik = wikidata_by_cik(ciks)
    for e in with_cik:
        hit = by_cik.get(e["cik"].strip().zfill(10))
        if hit:
            qid, lei = hit
            results[e["entity_id"]] = {
                "lei": lei, "wikidata_qid": qid, "method": "wikidata_cik"}
    found_qid = len(results)
    found_lei = sum(1 for v in results.values() if v["lei"])
    print(f"  matched: {found_qid} QIDs, {found_lei} LEIs")

    # 2) GLEIF fulltext fallback for entities still missing an LEI
    missing = [e for e in entities
               if not (e["lei"] or "").strip()
               and not results.get(e["entity_id"], {}).get("lei")]
    print(f"GLEIF: fulltext fallback for {len(missing)} entities "
          f"(exact-name acceptance only)...")
    gleif_found = 0
    for e in missing:
        try:
            cands = gleif_candidates(e["name"])
        except Exception as exc:   # one source hiccup never kills the run (R10.3)
            print(f"  WARN {e['entity_id']} {e['name']}: {exc}")
            time.sleep(GLEIF_SLEEP_S)
            continue
        lei = accept_gleif(terms[e["entity_id"]], cands)
        if lei:
            r = results.setdefault(e["entity_id"],
                                   {"lei": "", "wikidata_qid": "", "method": ""})
            r["lei"] = lei
            r["method"] = (r["method"] + "+gleif_exact_name").lstrip("+")
            gleif_found += 1
        time.sleep(GLEIF_SLEEP_S)
    print(f"  accepted: {gleif_found} LEIs")

    # 2b) map GLEIF-found LEIs back to QIDs (deterministic batch)
    lei_needing_qid = [v["lei"] for v in results.values()
                       if v["lei"] and not v["wikidata_qid"]]
    if lei_needing_qid:
        for lei, qid in wikidata_by_lei(lei_needing_qid).items():
            for v in results.values():
                if v["lei"] == lei and not v["wikidata_qid"]:
                    v["wikidata_qid"] = qid
                    v["method"] += "+wikidata_lei"

    # 3) GLEIF direct-parent relationships among watchlist LEIs (R6.5)
    lei_to_eid = {v["lei"]: eid for eid, v in results.items() if v["lei"]}
    for e in entities:   # hand-filled LEIs count too
        if (e["lei"] or "").strip():
            lei_to_eid.setdefault(e["lei"].strip(), e["entity_id"])
    print(f"GLEIF: direct-parent lookup for {len(lei_to_eid)} LEIs...")
    relationships = []
    for lei, child_eid in sorted(lei_to_eid.items()):
        try:
            parent_lei = gleif_direct_parent(lei)
        except Exception as exc:
            print(f"  WARN parent({lei}): {exc}")
            time.sleep(GLEIF_SLEEP_S)
            continue
        if parent_lei and parent_lei in lei_to_eid:
            parent_eid = lei_to_eid[parent_lei]
            if parent_eid != child_eid:
                relationships.append((parent_eid, child_eid))
        time.sleep(GLEIF_SLEEP_S)
    print(f"  watchlist parent/child pairs: {len(relationships)}")

    # 4) write generated seed CSVs (reviewed in PR; loaded by the seed loader)
    with open(IDENTIFIERS_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["entity_id", "lei", "wikidata_qid", "method", "verified_at"])
        for eid in sorted(results):
            v = results[eid]
            if v["lei"] or v["wikidata_qid"]:
                w.writerow([eid, v["lei"], v["wikidata_qid"], v["method"], today])
    with open(RELATIONSHIPS_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["parent_entity_id", "child_entity_id", "relationship_type",
                    "source", "verified_at"])
        for parent_eid, child_eid in sorted(set(relationships)):
            w.writerow([parent_eid, child_eid, "direct_parent", "gleif", today])

    total = len([v for v in results.values() if v["lei"] or v["wikidata_qid"]])
    print(f"\nwrote {IDENTIFIERS_CSV} ({total} entities)")
    print(f"wrote {RELATIONSHIPS_CSV} ({len(set(relationships))} pairs)")
    print("run python -m app.db.load_seeds to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
