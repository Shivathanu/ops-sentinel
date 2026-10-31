"""Incidents reported by the client-side CLI agent (cli/).

The agent watches a client's own automation (a job, a log file, an HTTP endpoint) and reports a failure here.
From then on the incident takes the same path as a database incident: LLM triage, a Slack card, a named human's
approval, an audit row. The difference is who applies the fix. The engine never runs or even sees the fix
command: it lives in the client's config file and runs on the client's machine. The engine records only the fix
id (see agent_actions.py), and the agent reports the outcome back through `record_result`.

Because the engine cannot run what it does not hold, a compromised engine or a prompt-injected error message
cannot make a client machine execute anything that is not already in that client's config.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from sentinel.agent_actions import FIX_ID_PATTERN, SOURCE_TYPE, format_action, parse_fix_id
from sentinel.db import get_connection
from sentinel.remediation import _utcnow, append_audit
from sentinel.slack_notifier import post_incident_card, update_incident_card
from sentinel.triage_agent import triage
from sentinel.watcher import create_incident_row, store_slack_ts, update_incident_triage

logger = logging.getLogger("sentinel.agent_events")


class FailureEvent(BaseModel):
    source: str = Field(min_length=1, max_length=100)
    job: str = Field(min_length=1, max_length=200)
    error: str = Field(max_length=4000)
    exit_code: int | None = None
    fix_id: str | None = Field(default=None, pattern=FIX_ID_PATTERN)


class EventResult(BaseModel):
    agent: str = Field(min_length=1, max_length=100)
    outcome: Literal["SUCCESS", "FAILED"]
    detail: str = Field(max_length=4000)
    attempts: int = Field(ge=1, le=20)


@dataclass(frozen=True)
class EventStatus:
    incident_id: int
    status: str
    fix_id: str | None


def open_incident(event: FailureEvent) -> int:
    """Creates the incident row synchronously so the agent gets an id back before triage finishes."""
    incident_id = create_incident_row(SOURCE_TYPE, 0)
    logger.info("agent source '%s' reported a failure of '%s' -> incident #%s", event.source, event.job, incident_id)
    return incident_id


async def triage_and_notify(incident_id: int, event: FailureEvent) -> None:
    """Runs triage and posts the approval card. Runs after the HTTP response, so failures are logged, not raised."""
    try:
        await _triage_and_notify(incident_id, event)
    except Exception:  # noqa: BLE001 — a background task has no caller to raise into
        logger.exception("could not triage or announce agent incident #%s", incident_id)


async def _triage_and_notify(incident_id: int, event: FailureEvent) -> None:
    error_context = {"source": event.source, "job": event.job, "exit_code": event.exit_code, "error": event.error}
    try:
        triage_result = await asyncio.to_thread(triage, error_context, [])
    except Exception:  # noqa: BLE001
        # The person approving still needs to see the failure when the model is down or returns bad JSON, so the
        # raw error stands in for the analysis, marked as such and rated HIGH.
        logger.exception("triage failed for agent incident #%s; announcing the raw error", incident_id)
        triage_result = {
            "business_impact": f"'{event.job}' failed. Automatic impact analysis was unavailable.",
            "root_cause": event.error[-1000:] or "No error output captured.",
            "risk_level": "HIGH",
        }

    # The model is only asked about the failure; the fix is always the one the client configured, never SQL.
    triage_result["suggested_fix_sql"] = (
        format_action(event.fix_id, event.source, event.job) if event.fix_id else None
    )
    await asyncio.to_thread(update_incident_triage, incident_id, triage_result)
    message_ts = await asyncio.to_thread(
        post_incident_card,
        incident_id=incident_id,
        source_type=f"AGENT FAILURE ({event.source})",
        business_impact=triage_result.get("business_impact", ""),
        root_cause=triage_result.get("root_cause", ""),
        suggested_fix_sql=triage_result["suggested_fix_sql"],
        risk_level=triage_result.get("risk_level", "HIGH"),
    )
    await asyncio.to_thread(store_slack_ts, incident_id, message_ts)


def get_status(incident_id: int) -> EventStatus | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT STATUS, SUGGESTED_FIX_SQL FROM INCIDENTS "
            "WHERE INCIDENT_ID = {incident_id!d} AND SOURCE_TYPE = {source_type}",
            {"incident_id": incident_id, "source_type": SOURCE_TYPE},
        ).fetchone()
    if row is None:
        return None
    return EventStatus(incident_id=incident_id, status=row[0], fix_id=parse_fix_id(row[1]))


def record_result(incident_id: int, result: EventResult) -> bool:
    """Closes an approved incident with the outcome the agent observed.

    Returns False, changing nothing, unless the incident is an agent incident that is still APPROVED. The status
    check is part of the UPDATE, so a replayed or forged report cannot reopen or overwrite a finished incident.
    """
    succeeded = result.outcome == "SUCCESS"
    with get_connection() as conn:
        closed = conn.execute(
            "UPDATE INCIDENTS SET STATUS = {status}, RESOLVED_TS = {resolved_ts} "
            "WHERE INCIDENT_ID = {incident_id!d} AND SOURCE_TYPE = {source_type} AND STATUS = 'APPROVED'",
            {
                "status": "REMEDIATED" if succeeded else "FAILED",
                "resolved_ts": _utcnow() if succeeded else None,
                "incident_id": incident_id,
                "source_type": SOURCE_TYPE,
            },
        )
        if closed.rowcount() == 0:
            logger.warning("ignoring agent result for incident #%s: not an approved agent incident", incident_id)
            return False
        fix_text, message_ts = conn.execute(
            "SELECT SUGGESTED_FIX_SQL, SLACK_MESSAGE_TS FROM INCIDENTS WHERE INCIDENT_ID = {incident_id!d}",
            {"incident_id": incident_id},
        ).fetchone()
        append_audit(
            conn, incident_id, fix_text or "", f"agent:{result.agent}", result.outcome,
            f"{result.detail} (attempts: {result.attempts})",
        )
    _update_card(incident_id, message_ts, result)
    return True


def _update_card(incident_id: int, message_ts: str | None, result: EventResult) -> None:
    """Best effort: the audit row is the record, so a Slack failure must not undo or fail the result."""
    if not message_ts:
        return
    icon = "✅" if result.outcome == "SUCCESS" else "⚠️"
    verb = "fix applied" if result.outcome == "SUCCESS" else "fix failed"
    try:
        update_incident_card(message_ts, f"{icon} Incident #{incident_id} — {verb} by agent '{result.agent}'")
    except Exception:  # noqa: BLE001
        logger.exception("could not update the Slack card for incident #%s", incident_id)
