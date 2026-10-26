"""Module C (dashboard half) — the Command Dashboard. Polls the engine's REST
API; keep it dumb on purpose, all logic lives in the engine."""
import os

import httpx
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

ENGINE_BASE_URL = os.getenv("ENGINE_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("OPS_SENTINEL_API_KEY", "")

st.set_page_config(page_title="Ops Sentinel", page_icon="🛡️", layout="wide")
st.title("🛡️ Ops Sentinel — Platform Command Center")


@st.cache_data(ttl=5)
def fetch(path: str) -> dict:
    with httpx.Client(timeout=5, headers={"X-API-Key": API_KEY}) as client:
        response = client.get(f"{ENGINE_BASE_URL}{path}")
        response.raise_for_status()
        return response.json()


col1, col2 = st.columns(2)
try:
    trust = fetch("/api/trust-score")["trust_score"]
    efficiency = fetch("/api/efficiency-score")["efficiency_score"]
    col1.metric("Platform Trust Score", f"{trust}%")
    col2.metric("Cloud Efficiency Rating", f"{efficiency}%")
except httpx.HTTPError:
    st.error(
        "Could not get data from the Ops Sentinel engine. Is `uvicorn ops_sentinel.main:app` running, "
        "and does OPS_SENTINEL_API_KEY match the engine's key?"
    )
    st.stop()

st.subheader("Recent Incidents")
incidents = fetch("/api/incidents")["incidents"]
if incidents:
    df = pd.DataFrame(incidents)
    st.dataframe(df, use_container_width=True)
else:
    st.info("No incidents yet. Run `python scripts/inject_demo_incident.py` to simulate one.")

st.caption("Auto-refreshes every 5 seconds — rerun the page or wrap in `st_autorefresh` for a live demo.")
