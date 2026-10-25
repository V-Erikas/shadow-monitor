import streamlit as st

from shadow.config import CATEGORIES, DB_PATH

st.set_page_config(page_title="Shadow-economy leads", layout="wide")
st.title("Shadow-economy leads: Vilnius")
st.caption("Leads for review, not accusations.")

if not DB_PATH.exists():
    st.info("No data yet. Run the pipeline: `python -m shadow.pipeline`")
    st.stop()

st.write("Categories:", ", ".join(c.label for c in CATEGORIES.values()))
