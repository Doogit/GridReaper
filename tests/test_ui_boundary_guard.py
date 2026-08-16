"""R10.9 module-separation guard: what may cross the UI/backend boundary.

R10.9 says source adapters, parsers, classifiers, license-play generation, and
UI rendering MUST be module-separated so source churn does not force UI changes.
This guard pins the boundary AS IT ACTUALLY IS rather than as anyone wishes it
were: every ``app.*`` import inside ``app/ui_web/`` must be either INSIDE the
boundary or a NAMED, justified exception, and every backwards edge (a backend
module importing ``app.ui_web``) must be named too.

Two properties, both load-bearing:

  * **No new crossing.** Any import not in the allow-list or the exception list
    fails the test. A guard shaped only to the crossings someone happened to be
    fixing would pass while the boundary was still open.
  * **No stale exception.** Every named exception must still exist. Closing one
    without removing it from this list fails the test, so the list cannot
    quietly overstate how much is left to do — and R10.9's status is readable
    straight off ``SANCTIONED_CROSSINGS`` and ``BACKWARDS_EDGES``.

**R10.9 is therefore PARTIAL, with three named exceptions and one backwards
edge, not MET.** Deciding whether ``app.db.connection``, ``app.digest`` and
``app.audit.precision`` are inside the boundary is an architectural ruling, not
a refactor; the exceptions below record the current answer with its rationale so
the ruling can be made against a true inventory. See the PR body.
"""
import ast
import os
import unittest

UI_PACKAGE = "app/ui_web"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# INSIDE the boundary: the UI's own package, and the read seam it is designed
# to read the backend through (R8.1-R8.3). app.ui.data is the ONE sanctioned
# door; it is stdlib-only and read-only apart from its declared write seams.
INSIDE_BOUNDARY = ("app.ui_web", "app.ui")

# Crossings that exist today, each with the reason it has not been closed.
# {(relative path, imported module prefix): rationale}
SANCTIONED_CROSSINGS = {
    ("app/ui_web/deps.py", "app.db.connection"):
        "FastAPI request-scoped connection dependency. Closing it means moving "
        "connection lifecycle behind app.ui.data, which owns no lifecycle "
        "today. Ruling needed: is app.db.connection infrastructure (inside) or "
        "backend (outside)?",
    ("app/ui_web/render.py", "app.audit.precision"):
        "SANCTIONED BY DESIGN, with a recorded rationale at the import site: "
        "precision returns computation dicts and these pure helpers reshape "
        "them into template-ready view dicts, carrying the rate-plus-n trust "
        "invariant to the DOM. precision.py is pure (math + datetime only, no "
        "DB, no network). A prior plan sanctioned this deliberately; do not "
        "reverse it silently.",
    ("app/ui_web/routes/digest.py", "app.db.connection"):
        "Reads DEFAULT_DB_PATH to locate the digest output directory. Same "
        "ruling as deps.py.",
    ("app/ui_web/routes/digest.py", "app.digest"):
        "Reads _digest_dir - a private helper - to list generated digests. "
        "Paired with the backwards edge below: app/digest.py already imports "
        "app/ui_web/, so digest and the UI are mutually dependent today.",
}

# Backend modules that import the UI package - the dependency running the wrong
# way. {(relative path, imported module prefix): rationale}
BACKWARDS_EDGES = {
    ("app/digest.py", "app.ui_web"):
        "app/digest.py calls render.card_view and templates.env so the digest "
        "and the web UI render one card definition. The cost is that a backend "
        "module imports Jinja2, which the stdlib-only backend rule otherwise "
        "forbids. Ruling needed: is the digest part of the UI layer?",
}


def _iter_python_files(rel_dir):
    root = os.path.join(REPO_ROOT, rel_dir)
    for dirpath, _dirnames, filenames in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for name in sorted(filenames):
            if name.endswith(".py"):
                full = os.path.join(dirpath, name)
                yield full, os.path.relpath(full, REPO_ROOT).replace("\\", "/")


def _imported_app_modules(path):
    """Every ``app.*`` module this file imports, however it spells the import."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app" or alias.name.startswith("app."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:       # relative import: inside the same package
                continue
            mod = node.module or ""
            if mod == "app" or mod.startswith("app."):
                # Record the FULLY QUALIFIED name each alias resolves to -
                # `from app.audit import precision` is a dependency on
                # app.audit.precision, not on the whole app.audit package, and
                # the exception list is written at that precision.
                for alias in node.names:
                    found.add(f"{mod}.{alias.name}")
    return found


def _matching_key(rel_path, module, table):
    for (path, prefix), _reason in table.items():
        if path == rel_path and (module == prefix
                                 or module.startswith(prefix + ".")):
            return (path, prefix)
    return None


class TestUiBackendBoundary(unittest.TestCase):
    """R10.9: app/ui_web/ reaches the backend through named doors only."""

    def test_no_unsanctioned_crossing(self):
        offenders = []
        for full, rel in _iter_python_files(UI_PACKAGE):
            for module in sorted(_imported_app_modules(full)):
                if module.startswith(INSIDE_BOUNDARY):
                    continue
                if _matching_key(rel, module, SANCTIONED_CROSSINGS) is None:
                    offenders.append(f"{rel} -> {module}")
        self.assertEqual(
            offenders, [],
            "New UI->backend import(s) crossing the R10.9 boundary. Route the "
            "value through app.ui.data, or add a NAMED, justified entry to "
            "SANCTIONED_CROSSINGS:\n  " + "\n  ".join(offenders))

    def test_no_stale_sanctioned_crossing(self):
        live = set()
        for full, rel in _iter_python_files(UI_PACKAGE):
            for module in _imported_app_modules(full):
                key = _matching_key(rel, module, SANCTIONED_CROSSINGS)
                if key is not None:
                    live.add(key)
        stale = sorted(set(SANCTIONED_CROSSINGS) - live)
        self.assertEqual(
            [f"{p} -> {m}" for p, m in stale], [],
            "Crossing(s) no longer exist. Remove them from "
            "SANCTIONED_CROSSINGS so R10.9's remaining exceptions stay a true "
            "count.")

    def test_backwards_edges_are_exactly_the_known_set(self):
        found = set()
        for full, rel in _iter_python_files("app"):
            if rel.startswith(UI_PACKAGE + "/") or rel.startswith("app/ui/"):
                continue
            for module in _imported_app_modules(full):
                if module.startswith("app.ui_web"):
                    found.add((rel, "app.ui_web"))
        self.assertEqual(
            sorted(found), sorted(BACKWARDS_EDGES),
            "The set of backend modules importing app/ui_web/ changed. A "
            "backend module importing the UI pulls Jinja2 into the "
            "stdlib-only backend; add it to BACKWARDS_EDGES with a rationale "
            "or remove the import.")

    def test_incident_tiers_is_read_through_the_data_seam(self):
        # the mechanical half of this chunk: no view imports the classifier
        for full, rel in _iter_python_files(UI_PACKAGE):
            self.assertNotIn(
                "app.classify.runner", _imported_app_modules(full),
                f"{rel} imports the classifier directly; read "
                "data.INCIDENT_TIERS instead (R10.9).")

    def test_the_data_seam_still_exports_the_tier_vocabulary(self):
        from app.ui import data
        self.assertEqual(
            data.INCIDENT_TIERS,
            ("confirmed", "corroborated", "unconfirmed_early_warning"))


if __name__ == "__main__":
    unittest.main()
