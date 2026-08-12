"""GridSignals — Signal Feed (home). R8.1.

STUB entry point for Phase A: injects the theme and renders an honest empty
state so `streamlit run app/ui/Home.py` and streamlit.testing AppTest have a
working entry point. B1 replaces this with the full scope-separated feed,
card component, feedback write path, keyset pagination, and 120s fragment.
"""
import streamlit as st

from app.db.connection import get_connection
from app.ui import data, theme


@st.cache_resource
def _conn():
    return get_connection()


def main():
    st.set_page_config(page_title="GridSignals — Signal Feed", layout="wide")
    theme.inject(st)
    st.title("GridSignals")
    st.caption("Signal Feed")

    conn = _conn()
    account = data.feed_page(conn, scope_group="account")
    sector = data.feed_page(conn, scope_group="sector")

    if not account:
        st.markdown(
            "<div class='gs-empty'>No account-scoped signals in this window — "
            "low-volume by design (R6.6). Broader sector and regulatory signals "
            "appear below once the feed is built.</div>",
            unsafe_allow_html=True)
    st.write(f"account signals: {len(account)} · sector/regulatory signals: "
             f"{len(sector)}  (feed rendering lands in B1)")


main()
