"""
FastAPI entrypoint. Three responsibilities live here:
  1. Start the background watcher loop on boot (steps 1-4 of the architecture).
  2. Receive, verify and authorize Slack's interactivity callback and act on it (step 5).
  3. Serve the small REST API the Streamlit dashboard polls (step 6), behind an API key.

Blocking Exasol calls are offloaded with `asyncio.to_thread` so the event loop keeps serving requests.
Slack requires an HTTP response within 3 seconds and retries otherwise, so the slow remediation runs in a
background task after the interaction is acknowledged; the atomic claim in remediation.py makes a retried
delivery harmless.
"""
import asyncio
import contextlib
import json
import logging
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from slack_sdk.signature import SignatureVerifier

from ops_sentinel.config import settings
from ops_sentinel.remediation import execute_remediation, reject_incident
from ops_sentinel.scoring import compute_efficiency_score, compute_trust_score, recent_incidents
from ops_sentinel.security import require_api_key, startup_warnings
from ops_sentinel.slack_notifier import update_incident_card
from ops_sentinel.watcher import watch_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ops_sentinel.main")

APPROVE_ACTION = "approve_incident"
REJECT_ACTION = "reject_incident"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    for warning in startup_warnings():
        logger.warning(warning)
    # Keep a reference: the event loop only holds weak references to tasks, so an unreferenced watcher can be
    # garbage-collected mid-run.
    watcher = asyncio.create_task(watch_loop())
    try:
        yield
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


app = FastAPI(title="Ops Sentinel Engine", lifespan=lifespan)

# Slack isn't required for the detect/investigate part of the loop, only for the approval step. Disable that
# one endpoint instead of crashing the engine when Slack hasn't been configured yet.
verifier = SignatureVerifier(settings.slack_signing_secret) if settings.slack_signing_secret else None


@dataclass(frozen=True)
class SlackAction:
    action_id: str
    incident_id: int
    user_id: str
    username: str
    message_ts: str

    @property
    def actor(self) -> str:
        """Identity recorded in the audit trail; the ID is stable even if the username changes."""
        return f"{self.username} ({self.user_id})"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/trust-score", dependencies=[Depends(require_api_key)])
async def trust_score() -> dict[str, float]:
    return {"trust_score": await asyncio.to_thread(compute_trust_score)}


@app.get("/api/efficiency-score", dependencies=[Depends(require_api_key)])
async def efficiency_score() -> dict[str, float]:
    return {"efficiency_score": await asyncio.to_thread(compute_efficiency_score)}


@app.get("/api/incidents", dependencies=[Depends(require_api_key)])
async def incidents() -> dict[str, list[dict]]:
    return {"incidents": await asyncio.to_thread(recent_incidents)}


@app.post("/slack/interactions")
async def slack_interactions(request: Request, background_tasks: BackgroundTasks) -> dict[str, bool]:
    if verifier is None:
        raise HTTPException(status_code=503, detail="Slack is not configured on this engine (SLACK_SIGNING_SECRET unset)")
    body = await request.body()
    if not verifier.is_valid_request(body, dict(request.headers)):
        raise HTTPException(status_code=401, detail="invalid Slack signature")

    action = _parse_slack_action(body)
    if action.user_id not in settings.approver_ids():
        logger.warning(
            "refusing %s on incident #%s from %s: not in SLACK_APPROVER_IDS", action.action_id, action.incident_id, action.actor
        )
        raise HTTPException(status_code=403, detail="you are not an authorized approver")

    if action.action_id == APPROVE_ACTION:
        background_tasks.add_task(_process_approval, action)
    elif action.action_id == REJECT_ACTION:
        background_tasks.add_task(_process_rejection, action)
    else:
        logger.warning("unknown Slack action_id %r from %s", action.action_id, action.actor)
        raise HTTPException(status_code=400, detail="unknown action")
    return {"ok": True}


def _parse_slack_action(body: bytes) -> SlackAction:
    try:
        form = urllib.parse.parse_qs(body.decode())
        payload = json.loads(form["payload"][0])
        action = payload["actions"][0]
        return SlackAction(
            action_id=action["action_id"],
            incident_id=int(action["value"]),
            user_id=payload["user"]["id"],
            username=payload["user"].get("username", payload["user"]["id"]),
            message_ts=payload["message"]["ts"],
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("malformed Slack interaction payload: %r", exc)
        raise HTTPException(status_code=400, detail="malformed Slack interaction payload") from exc


def _process_approval(action: SlackAction) -> None:
    try:
        success, detail = execute_remediation(action.incident_id, approved_by=action.actor)
        icon = "✅" if success else "⚠️"
        update_incident_card(
            action.message_ts, f"{icon} Incident #{action.incident_id} — {detail} (approved by @{action.username})"
        )
    except Exception:
        # Runs after the response was sent, so log instead of raising into nowhere.
        logger.exception("approval processing failed for incident #%s", action.incident_id)


def _process_rejection(action: SlackAction) -> None:
    try:
        reject_incident(action.incident_id)
        update_incident_card(action.message_ts, f"❌ Incident #{action.incident_id} rejected by @{action.username}")
    except Exception:
        logger.exception("rejection processing failed for incident #%s", action.incident_id)
