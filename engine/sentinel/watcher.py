"""
Step 1-4 of the loop in one background asyncio task: poll Exasol for new
FAILED pipeline runs / cost-spike queries, turn each into an INCIDENT row,
run it through MCP + the LLM, and post the Slack approval card.

CRITICAL FIX: the previous version called synchronous pyexasol, LLM, and
Slack SDK functions directly inside `async def` functions with no
`asyncio.to_thread(...)`. Since this task runs inside the same event loop
that serves the FastAPI app, every one of those blocking calls froze
`/api/*` and `/slack/interactions` for its full duration — a single slow LLM
call meant the dashboard and the Slack approval button both stopped
responding until it finished. Every blocking call below is now wrapped in
`asyncio.to_thread`, so the detection loop runs on worker threads and the
event loop stays free to serve HTTP requests throughout.

Polling (not a DB trigger) is the deliberately simple choice for a weekend
build — it needs zero extra infrastructure beyond what the starter kit
already gives you. If you have time left, swapping this for the Exasol
Scheduler add-on (SQL jobs as rows in a table) is a nice stretch goal that
keeps the "no polling daemon" story even cleaner.
"""
import asyncio
import logging
from datetime import datetime

from sentinel.config import settings
from sentinel.db import get_connection
from sentinel.mcp_client import get_error_context, get_lineage_for_table
from sentinel.slack_notifier import post_incident_card
from sentinel.triage_agent import triage

logger = logging.getLogger("sentinel.watcher")


async def watch_loop():
    while True:
        try:
            pipeline_failures = await asyncio.to_thread(_fetch_new_pipeline_failures)
            cost_spikes = await asyncio.to_thread(_fetch_new_cost_spikes)

            for log_id, pipeline_id, target_table in pipeline_failures:
                await _handle_incident_safely("PIPELINE_FAILURE", log_id, pipeline_id, target_table)

            for query_id, table_name in cost_spikes:
                await _handle_incident_safely("COST_SPIKE", query_id, None, table_name)

        except Exception:  # noqa: BLE001 — never let one bad cycle kill the watcher
            logger.exception("watch cycle failed")
        await asyncio.sleep(settings.watch_interval_seconds)


def _fetch_new_pipeline_failures() -> list[tuple]:
    """Plain sync function — called via asyncio.to_thread, never awaited directly."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT LOG_ID, PIPELINE_ID, TARGET_TABLE FROM PIPELINE_LOGS "
            "WHERE STATUS = 'FAILED' AND LOG_ID NOT IN "
            "  (SELECT SOURCE_REF_ID FROM INCIDENTS WHERE SOURCE_TYPE = 'PIPELINE_FAILURE')"
        ).fetchall()


def _fetch_new_cost_spikes() -> list[tuple]:
    # pyexasol binds params by name ({x}), not positional ? markers, and its
    # default conversion quotes values as strings — {x!d} keeps numbers
    # unquoted, which Exasol requires for DECIMAL comparisons.
    with get_connection() as conn:
        return conn.execute(
            "SELECT QUERY_ID, TABLE_NAME FROM QUERY_METRICS "
            "WHERE ESTIMATED_COST_USD > {threshold!d} AND QUERY_ID NOT IN "
            "  (SELECT SOURCE_REF_ID FROM INCIDENTS WHERE SOURCE_TYPE = 'COST_SPIKE')",
            {"threshold": settings.cost_spike_threshold_usd},
        ).fetchall()


async def _handle_incident_safely(source_type: str, source_ref_id: int,
                                   pipeline_id: str | None, table_name: str | None):
    """Wraps _handle_incident so one bad incident (LLM error, bad lineage
    row, Slack outage) can't take down the rest of the batch — each
    incident is detected/triaged/posted independently."""
    try:
        await _handle_incident(source_type, source_ref_id, pipeline_id, table_name)
    except Exception:  # noqa: BLE001
        logger.exception("failed to process %s source_ref_id=%s", source_type, source_ref_id)


async def _handle_incident(source_type: str, source_ref_id: int,
                            pipeline_id: str | None, table_name: str | None):
    incident_id = await asyncio.to_thread(create_incident_row, source_type, source_ref_id)
    logger.info("detected %s -> incident #%s", source_type, incident_id)

    error_context = {}
    if source_type == "PIPELINE_FAILURE":
        error_context = await get_error_context(pipeline_id, source_ref_id)  # already async: real MCP I/O
    lineage = await get_lineage_for_table(table_name) if table_name else []

    triage_result = await asyncio.to_thread(triage, error_context, lineage)
    await asyncio.to_thread(update_incident_triage, incident_id, triage_result)

    message_ts = await asyncio.to_thread(
        post_incident_card,
        incident_id=incident_id,
        source_type=source_type,
        business_impact=triage_result.get("business_impact", ""),
        root_cause=triage_result.get("root_cause", ""),
        suggested_fix_sql=triage_result.get("suggested_fix_sql"),
        risk_level=triage_result.get("risk_level", "HIGH"),
    )
    await asyncio.to_thread(store_slack_ts, incident_id, message_ts)


def create_incident_row(source_type: str, source_ref_id: int) -> int:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO INCIDENTS (SOURCE_TYPE, SOURCE_REF_ID, CREATED_TS, STATUS) "
            "VALUES ({source_type}, {source_ref_id!d}, {created_ts}, 'DETECTED')",
            {"source_type": source_type, "source_ref_id": source_ref_id, "created_ts": datetime.utcnow()},
        )
        return conn.execute("SELECT MAX(INCIDENT_ID) FROM INCIDENTS").fetchone()[0]


def update_incident_triage(incident_id: int, triage_result: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE INCIDENTS SET STATUS = 'AWAITING_APPROVAL', BUSINESS_IMPACT = {business_impact}, "
            "ROOT_CAUSE = {root_cause}, SUGGESTED_FIX_SQL = {suggested_fix_sql}, RISK_LEVEL = {risk_level} "
            "WHERE INCIDENT_ID = {incident_id!d}",
            {
                "business_impact": triage_result.get("business_impact"),
                "root_cause": triage_result.get("root_cause"),
                "suggested_fix_sql": triage_result.get("suggested_fix_sql"),
                "risk_level": triage_result.get("risk_level"),
                "incident_id": incident_id,
            },
        )


def store_slack_ts(incident_id: int, message_ts: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE INCIDENTS SET SLACK_MESSAGE_TS = {message_ts} WHERE INCIDENT_ID = {incident_id!d}",
            {"message_ts": message_ts, "incident_id": incident_id},
        )
