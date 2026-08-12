"""GridSignals FastAPI web UI (R8.9 UI port).

Server-rendered replacement for the retired Streamlit pages, built on FastAPI +
HTMX + Tailwind for executive-grade visual control. This package is a read-only
reader over app/ui/data.py: it adds no queries and writes only through data.py's
existing helpers. FastAPI/uvicorn/jinja2 are the sanctioned UI-layer third-party
exception; the backend stays stdlib-only.
"""
