"""
FastAPI entrypoint. Three responsibilities live here:
  1. Start the background watcher loop on boot (step 1-4 of the architecture).
  2. Receive + verify Slack's interactivity callback and act on it (step 5).
  3. Serve the small REST API the Streamlit dashboard polls (step 6).

CRITICAL FIX: the GET endpoints used to call synchronous Exasol-backed
functions directly inside `async def` handlers, blocking the whole event
loop on every dashboard refresh. They're now offloaded with
`asyncio.to_thread`. The Slack endpoint had a second, related problem:
Slack requires an HTTP response within 3 seconds or it treats the
interaction as failed and retries the same payload — but `execute_remediation`
can take a while (an LLM call isn't involved here, but a slow Exasol write
could still blow the window). It now acks immediately and does the actual
remediation via `BackgroundTasks`, which also offloads the sync call to a
worker thread for free.
"""
import asyncio
import json
import logging
import urllib.parse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from slack_sdk.signature import SignatureVerifier

from ops_sentinel.config import settings
from ops_sentinel.remediation import execute_remediation, reject_incident
from ops_sentinel.scoring import compute_efficiency_score, compute_trust_score, recent_incidents
from ops_sentinel.slack_notifier import update_incident_card
from ops_sentinel.watcher import watch_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ops_sentinel.main")

app = FastAPI(title="Ops Sentinel Engine")

# Slack isn't required for the detect/triage/investigate part of the loop to
# run — only for the approval step. Don't crash the whole engine just
# because Slack hasn't been configured yet; disable that one endpoint
# instead and say so clearly in the logs.
verifier = SignatureVerifier(settings.slack_signing_secret) if settings.slack_signing_secret else None
if verifier is None:
    logging.getLogger("ops_sentinel.main").warning(
        "SLACK_SIGNING_SECRET not set — /slack/interactions will reject all requests "
        "until Slack is configured. Detection and MCP investigation still run normally."
    )


@app.on_event("startup")
async def start_watcher():
    asyncio.create_task(watch_loop())


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/trust-score")
async def trust_score():
    return {"trust_score": await asyncio.to_thread(compute_trust_score)}


@app.get("/api/efficiency-score")
async def efficiency_score():
    return {"efficiency_score": await asyncio.to_thread(compute_efficiency_score)}


@app.get("/api/incidents")
async def incidents():
    return {"incidents": await asyncio.to_thread(recent_incidents)}


@app.post("/slack/interactions")
async def slack_interactions(request: Request, background_tasks: BackgroundTasks):
    if verifier is None:
        raise HTTPException(status_code=503, detail="Slack is not configured on this engine (SLACK_SIGNING_SECRET unset)")
    body = await request.body()
    if not verifier.is_valid_request(body, dict(request.headers)):
        raise HTTPException(status_code=401, detail="invalid Slack signature")

    form = urllib.parse.parse_qs(body.decode())
    payload = json.loads(form["payload"][0])

    action = payload["actions"][0]
    action_id = action["action_id"]
    incident_id = int(action["value"])
    user = payload["user"]["username"]
    message_ts = payload["message"]["ts"]

    # Ack Slack within its 3-second window immediately; do the actual work
    # (a sync Exasol write) in the background. BackgroundTasks runs sync
    # callables in a worker thread automatically, so this also keeps the
    # event loop free without an explicit asyncio.to_thread here.
    if action_id == "approve_incident":
        background_tasks.add_task(_process_approval, incident_id, user, message_ts)
    elif action_id == "reject_incident":
        background_tasks.add_task(_process_rejection, incident_id, user, message_ts)

    return {"ok": True}


def _process_approval(incident_id: int, user: str, message_ts: str) -> None:
    try:
        success, detail = execute_remediation(incident_id, approved_by=user)
        icon = "\u2705" if success else "\u26a0\ufe0f"
        update_incident_card(message_ts, f"{icon} Incident #{incident_id} — {detail} (approved by @{user})")
    except Exception:  # noqa: BLE001 — this runs off-request, so log instead of raising into nowhere
        logger.exception("approval processing failed for incident #%s", incident_id)


def _process_rejection(incident_id: int, user: str, message_ts: str) -> None:
    try:
        reject_incident(incident_id)
        update_incident_card(message_ts, f"\u274c Incident #{incident_id} rejected by @{user}")
    except Exception:  # noqa: BLE001
        logger.exception("rejection processing failed for incident #%s", incident_id)
