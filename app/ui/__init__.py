"""GridSignals UI data seam (R8.1-R8.3, R8.9).

Holds `data.py`, the shared read/write seam the FastAPI + HTMX UI
(`app/ui_web/`) binds to. The UI is a reader: it writes only feedback rows
(R9.1), review-queue dispositions, and human entity-match decisions (R8.2) -
never signals, snapshots, or config. Cards read license_play_snapshots, never
live license_facts (R7.6). The Streamlit pages that formerly lived here were
removed at the Chunk 7 cutover; `data.py` itself stays stdlib-only.
"""
