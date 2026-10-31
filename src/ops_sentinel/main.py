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

from ops_sentinel import agent_events
from ops_sentinel.api.v1 import router as v1_router
from ops_sentinel.config import settings
from ops_sentinel.platform import get_platform
from ops_sentinel.remediation import execute_remediation, reject_incident
from ops_sentinel.scoring import (
    compute_efficiency_score,
    compute_trust_score,
    incident_timeline,
    recent_incidents,
)
from ops_sentinel.security import require_api_key, startup_warnings
from ops_sentinel.slack_notifier import update_incident_card
from ops_sentinel.watcher import watch_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ops_sentinel.main")

APPROVE_ACTION = "approve_incident"
REJECT_ACTION = "reject_incident"


def _start_platform() -> None:
    """With a workspace spec configured: migrate the platform schema (if enabled), validate plugins, record specs.
    A failure here stops the process, so an orchestrator restarts it instead of serving a half-configured engine."""
    platform = get_platform()
    if platform is None:
        logger.info("no workspace spec (OPS_SENTINEL_SPEC_FILE); serving the v1 /api endpoints only")
        return
    if settings.auto_migrate:
        from ops_sentinel.store.migrate import migrate_from_settings

        migrate_from_settings()
    problems = platform.validate()
    if problems:
        raise RuntimeError("invalid workspace configuration:\n  - " + "\n  - ".join(problems))
    platform.sync_workspaces()
    logger.info("platform ready: workspaces %s", ", ".join(platform.workspaces.names()))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    for warning in startup_warnings():
        logger.warning(warning)
    await asyncio.to_thread(_start_platform)
    # Keep a reference: the event loop only holds weak references to tasks, so an unreferenced watcher can be
    # garbage-collected mid-run.
    watcher = asyncio.create_task(watch_loop()) if settings.watcher_enabled else None
    if watcher is None:
        logger.info("database watcher disabled (OPS_SENTINEL_WATCHER_ENABLED=false); serving agent incidents only")
    try:
        yield
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher


app = FastAPI(title="Ops Sentinel Engine", lifespan=lifespan)
app.include_router(v1_router)

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


@app.get("/api/incidents/{incident_id}/timeline", dependencies=[Depends(require_api_key)])
async def incident_timeline_route(incident_id: int) -> dict:
    timeline = await asyncio.to_thread(incident_timeline, incident_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="no incident with that id")
    return timeline


@app.post("/api/events", status_code=202, dependencies=[Depends(require_api_key)])
async def report_event(event: agent_events.FailureEvent, background_tasks: BackgroundTasks) -> dict[str, int]:
    """A client-side agent reports a failure. Triage and the Slack card follow in the background."""
    incident_id = await asyncio.to_thread(agent_events.open_incident, event)
    background_tasks.add_task(agent_events.triage_and_notify, incident_id, event)
    return {"incident_id": incident_id}


@app.get("/api/events/{incident_id}", dependencies=[Depends(require_api_key)])
async def event_status(incident_id: int) -> dict[str, object]:
    status = await asyncio.to_thread(agent_events.get_status, incident_id)
    if status is None:
        raise HTTPException(status_code=404, detail="no agent incident with that id")
    return {"incident_id": status.incident_id, "status": status.status, "fix_id": status.fix_id}


@app.post("/api/events/{incident_id}/result", dependencies=[Depends(require_api_key)])
async def event_result(incident_id: int, result: agent_events.EventResult) -> dict[str, bool]:
    recorded = await asyncio.to_thread(agent_events.record_result, incident_id, result)
    if not recorded:
        raise HTTPException(status_code=409, detail="incident is not an approved agent incident")
    return {"recorded": True}


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
        icon = "âœ…" if success else "âš ï¸"
        update_incident_card(
            action.message_ts, f"{icon} Incident #{action.incident_id} â€” {detail} (approved by @{action.username})"
        )
    except Exception:
        # Runs after the response was sent, so log instead of raising into nowhere.
        logger.exception("approval processing failed for incident #%s", action.incident_id)


def _process_rejection(action: SlackAction) -> None:
    try:
        reject_incident(action.incident_id)
        update_incident_card(action.message_ts, f"âŒ Incident #{action.incident_id} rejected by @{action.username}")
    except Exception:
        logger.exception("rejection processing failed for incident #%s", action.incident_id)
