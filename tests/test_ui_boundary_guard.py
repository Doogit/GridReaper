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

**R10.9 is MET: both lists are EMPTY.** The operator's ruling (2026-08-15):

  * ``app.db.connection`` is INSIDE the boundary. It is infrastructure and the
    composition root — a connection factory plus the packaged DB path, with no
    source adapter, parser, classifier or license-play logic in it. R10.9 is
    about source churn not forcing UI changes; where the SQLite file lives is
    not source churn.
  * ``app.digest`` was NOT a backend module. It renders Jinja templates through
    ``app.ui_web.render``, so it belongs in ``app/ui_web/`` and now lives at
    ``app/ui_web/digest.py``. That killed the only backwards edge — and with it
    the Jinja2 import inside the stdlib-only backend tree — and the
    ``routes/digest.py -> app.digest`` crossing along with it.
  * ``app.audit.precision`` is OUTSIDE and no longer crossed. The precision
    COMPUTATIONS moved behind the read seam as ``data.precision_report``; the
    ``render.precision_*`` helpers now only format the dicts it returns. The
    fix is a moved computation, not a re-export: nothing in ``app/ui/data.py``
    forwards precision's module, functions or names to the view.

An empty exception list is the point. Adding an entry here is an architectural
ruling, not a refactor — it needs the operator, not a passing test.
"""
import ast
import os
import unittest

UI_PACKAGE = "app/ui_web"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# INSIDE the boundary: the UI's own package, the read seam it is designed to
# read the backend through (R8.1-R8.3), and the connection infrastructure the
# composition root needs. app.ui.data is the ONE door to backend LOGIC; it is
# stdlib-only and read-only apart from its declared write seams.
INSIDE_BOUNDARY = ("app.ui_web", "app.ui", "app.db.connection")

# Crossings that exist today, each with the reason it has not been closed.
# {(relative path, imported module prefix): rationale}
# EMPTY: R10.9 is met. Read the module docstring before adding an entry.
SANCTIONED_CROSSINGS = {}

# Backend modules that import the UI package - the dependency running the wrong
# way. {(relative path, imported module prefix): rationale}
# EMPTY: the dependency runs one way. A backend module importing app/ui_web/
# would pull Jinja2 into the stdlib-only backend; that is what moving the digest
# into app/ui_web/ removed.
BACKWARDS_EDGES = {}


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

    def test_precision_is_computed_behind_the_data_seam(self):
        # The anti-gaming half of closing the app.audit.precision crossing: the
        # view formats computed dicts and must not reach the computation module
        # by pulling it back out of the data seam (``data.precision.…`` or
        # ``from app.ui.data import precision``) — that is the same crossing
        # wearing a different import, and the AST guard above would not see it.
        for full, rel in _iter_python_files(UI_PACKAGE):
            with open(full, encoding="utf-8") as fh:
                src = fh.read()
            for gamed in ("data.precision.", "data import precision"):
                self.assertNotIn(
                    gamed, src,
                    f"{rel} reaches app.audit.precision through app.ui.data. "
                    "Read the computed dicts from data.precision_report and "
                    "format them (R10.9).")
        from app.ui import data
        self.assertTrue(callable(data.precision_report))

    def test_the_data_seam_still_exports_the_tier_vocabulary(self):
        from app.ui import data
        self.assertEqual(
            data.INCIDENT_TIERS,
            ("confirmed", "corroborated", "unconfirmed_early_warning"))


if __name__ == "__main__":
    unittest.main()
