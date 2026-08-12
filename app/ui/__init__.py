"""GridSignals Streamlit UI (R8.1-R8.3, R8.9).

The ONE place third-party imports (streamlit) are allowed. The app is a
reader: it writes only feedback rows (R9.1), review-queue dispositions, and
human entity-match decisions (R8.2) - never signals, snapshots, or config.
Cards read license_play_snapshots, never live license_facts (R7.6).
"""
