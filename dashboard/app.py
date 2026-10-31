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


STATUS_ICON = {
    "DETECTED": "🔎",
    "TRIAGED": "🧠",
    "AWAITING_APPROVAL": "⏳",
    "APPROVED": "✅",
    "REJECTED": "🚫",
    "REMEDIATED": "🛠️",
    "FAILED": "❌",
}


def render_timeline(data: dict) -> None:
    """Renders one incident's lifecycle as a vertical timeline: detection, each
    remediation attempt in order, then resolution — built from INCIDENTS /
    REMEDIATION_ACTIONS timestamps rather than a separate event log."""
    incident = data["incident"]
    st.markdown(f"**Incident #{incident['incident_id']}** — {incident['source_type']}, risk: {incident['risk_level']}")

    st.markdown(f"{STATUS_ICON.get('DETECTED', '•')} **{incident['created_ts']}** — Detected")
    if incident["business_impact"]:
        st.caption(f"Impact: {incident['business_impact']}")
    if incident["root_cause"]:
        st.caption(f"Root cause: {incident['root_cause']}")

    for action in data["actions"]:
        icon = STATUS_ICON.get(action["outcome"], "•")
        st.markdown(f"{icon} **{action['executed_ts']}** — Remediation by {action['executed_by']}: {action['outcome']}")
        if action["outcome_detail"]:
            st.caption(action["outcome_detail"])

    final_icon = STATUS_ICON.get(incident["status"], "•")
    if incident["resolved_ts"]:
        st.markdown(f"{final_icon} **{incident['resolved_ts']}** — Resolved ({incident['status']})")
    else:
        st.markdown(f"{final_icon} _Currently: {incident['status']}_")


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

    st.subheader("Incident Timeline")
    labels = {
        row["incident_id"]: f"#{row['incident_id']} — {row['source_type']} ({row['status']})" for row in incidents
    }
    selected_id = st.selectbox(
        "Pick an incident", options=list(labels), format_func=lambda i: labels[i]
    )
    render_timeline(fetch(f"/api/incidents/{selected_id}/timeline"))
else:
    st.info("No incidents yet. Run `python scripts/inject_demo_incident.py` to simulate one.")

st.caption("Auto-refreshes every 5 seconds — rerun the page or wrap in `st_autorefresh` for a live demo.")
