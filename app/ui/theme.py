"""The single injected CSS block for the GridSignals UI (R8.9).

config.toml sets the dark base; this module carries the one custom CSS block
every page injects at top via ``inject(st)``. Card HTML in components.py uses
these classes so the look is defined in exactly one place. B1 owns extensions
to this block during the feed build; other pages only call ``inject``.
"""

CSS = """
:root {
  --gs-bg: #0e1117;
  --gs-card: #161b22;
  --gs-card-border: #2b3138;
  --gs-text: #e6edf3;
  --gs-muted: #8b949e;
  --gs-accent: #4da3ff;
  --gs-crit: #f85149;
  --gs-high: #db6d28;
  --gs-mod: #d29922;
  --gs-low: #3fb950;
  --gs-chip-bg: #21262d;
  --gs-warn-bg: #3d2b0f;
  --gs-warn-border: #9e6a1f;
}

.gs-card {
  position: relative;
  background: var(--gs-card);
  border: 1px solid var(--gs-card-border);
  border-radius: 10px;
  padding: 14px 16px 12px 20px;
  margin-bottom: 14px;
  overflow: hidden;
}
/* severity strip (score band) down the left edge */
.gs-card::before {
  content: "";
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 5px;
  background: var(--gs-mod);
}
.gs-card.sev-critical::before { background: var(--gs-crit); }
.gs-card.sev-high::before     { background: var(--gs-high); }
.gs-card.sev-moderate::before { background: var(--gs-mod); }
.gs-card.sev-low::before      { background: var(--gs-low); }
.gs-card.status-superseded, .gs-card.status-decayed { opacity: 0.72; }

.gs-headline { font-size: 1.02rem; font-weight: 600; color: var(--gs-text);
  margin: 0 0 4px 0; line-height: 1.35; }
.gs-meta { font-size: 0.8rem; color: var(--gs-muted); margin-bottom: 8px; }

/* decay bar: current score vs the signal's fresh base_strength x fits */
.gs-decay-wrap { height: 6px; background: var(--gs-chip-bg); border-radius: 3px;
  overflow: hidden; margin: 8px 0; }
.gs-decay-fill { height: 100%; background: var(--gs-accent); }

.gs-badges { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.gs-badge { font-size: 0.72rem; padding: 2px 8px; border-radius: 10px;
  background: var(--gs-chip-bg); color: var(--gs-text); border: 1px solid var(--gs-card-border); }
.gs-badge.nonprimary { background: #3d2b0f; border-color: var(--gs-warn-border);
  color: #f0c674; }               /* R4.3 distinct non-primary badge */
.gs-badge.scope { background: #12233b; border-color: #274b7a; color: #9cc6ff; }
.gs-badge.coverage-dark { background: #2d2233; border-color: #6a4b7a; color: #d6b3f0; }

.gs-chip { font-size: 0.74rem; padding: 2px 8px; border-radius: 6px;
  background: var(--gs-chip-bg); color: var(--gs-text); margin-right: 4px; }

.gs-gov-caution { background: var(--gs-warn-bg); border: 1px solid var(--gs-warn-border);
  color: #f0c674; font-size: 0.8rem; padding: 6px 10px; border-radius: 6px; margin: 8px 0; }

.gs-evidence { font-size: 0.82rem; color: var(--gs-text); border-left: 2px solid var(--gs-card-border);
  padding-left: 10px; margin: 6px 0; }
.gs-locator { color: var(--gs-muted); font-size: 0.74rem; }

.gs-divider { display: flex; align-items: center; gap: 10px; margin: 18px 0 10px 0;
  color: var(--gs-muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.06em; }
.gs-divider::before, .gs-divider::after { content: ""; flex: 1; height: 1px; background: var(--gs-card-border); }

.gs-empty { background: var(--gs-card); border: 1px dashed var(--gs-card-border);
  border-radius: 10px; padding: 20px; color: var(--gs-muted); text-align: center; }
"""


def inject(st):
    """Inject the single CSS block. Safe to call once per page render."""
    st.markdown(f"<style>{CSS}</style>", unsafe_allow_html=True)
