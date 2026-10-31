"""Persists agent runs to Exasol: AGENT_RUNS (one per agent, subagents linked to their parent), AGENT_STEPS (every
model turn and tool call), EVIDENCE (full tool output behind truncated summaries), and the incident timeline."""
import logging
from typing import Any

from sentinel.agents.runtime import AgentResult
from sentinel.agents.tools import ToolContext
from sentinel.core.models import new_id, utcnow
from sentinel.store.exasol import Connection, ConnectionFactory, run_in_transaction, to_json, ts
from sentinel.store.incidents import add_event

logger = logging.getLogger("sentinel.agent_runs")


class ExasolRunRecorder:
    """Recording never breaks a run: a failed write is logged and the agent carries on."""

    def __init__(self, connect: ConnectionFactory):
        self._connect = connect
        self._contexts: dict[str, ToolContext] = {}

    def _write(self, work) -> None:
        try:
            run_in_transaction(self._connect, work)
        except Exception:  # noqa: BLE001
            logger.warning("could not record agent activity", exc_info=True)

    def start(self, ctx: ToolContext, agent: str, model: str, parent_run_id: str | None) -> str:
        run_id = new_id()
        self._contexts[run_id] = ctx

        def work(conn: Connection) -> None:
            conn.execute(
                "INSERT INTO AGENT_RUNS (RUN_ID, WORKSPACE_ID, INCIDENT_ID, PARENT_RUN_ID, AGENT_NAME, MODEL, STATUS, "
                "INPUT_TOKENS, OUTPUT_TOKENS, COST_USD, STARTED_AT) VALUES ({run_id}, {workspace_id}, {incident_id}, "
                "{parent}, {agent}, {model}, 'running', 0, 0, 0, {now})",
                {"run_id": run_id, "workspace_id": ctx.workspace_id, "incident_id": ctx.incident_id,
                 "parent": parent_run_id, "agent": agent, "model": model, "now": ts(utcnow())},
            )
            if ctx.incident_id:
                add_event(conn, ctx.workspace_id, ctx.incident_id, "agent.started", f"agent:{agent}",
                          {"run_id": run_id, "model": model, "parent_run_id": parent_run_id})

        self._write(work)
        return run_id

    def step(self, run_id: str, step_no: int, kind: str, *, tool: str | None = None, arguments: Any = None,
             summary: str = "", evidence: str | None = None, duration_ms: int = 0) -> None:
        ctx = self._contexts.get(run_id)

        def work(conn: Connection) -> None:
            evidence_id = None
            if evidence and ctx is not None:
                evidence_id = new_id()
                conn.execute(
                    "INSERT INTO EVIDENCE (EVIDENCE_ID, WORKSPACE_ID, INCIDENT_ID, SOURCE_NAME, CONTENT_TYPE, CONTENT, "
                    "CREATED_AT) VALUES ({id}, {workspace_id}, {incident_id}, {source}, 'text/plain', {content}, {now})",
                    {"id": evidence_id, "workspace_id": ctx.workspace_id, "incident_id": ctx.incident_id,
                     "source": f"tool:{tool}", "content": evidence[:2_000_000], "now": ts(utcnow())},
                )
            conn.execute(
                "INSERT INTO AGENT_STEPS (STEP_ID, RUN_ID, STEP_NO, KIND, TOOL_NAME, ARGUMENTS, RESULT_SUMMARY, "
                "EVIDENCE_ID, DURATION_MS, CREATED_AT) VALUES ({id}, {run_id}, {step_no!d}, {kind}, {tool}, {arguments}, "
                "{summary}, {evidence_id}, {duration!d}, {now})",
                {"id": new_id(), "run_id": run_id, "step_no": step_no, "kind": kind, "tool": tool,
                 "arguments": None if arguments is None else to_json(arguments)[:200_000],
                 "summary": summary[:200_000], "evidence_id": evidence_id, "duration": duration_ms, "now": ts(utcnow())},
            )
            if kind == "tool" and ctx is not None and ctx.incident_id:
                add_event(conn, ctx.workspace_id, ctx.incident_id, "agent.tool", f"run:{run_id}",
                          {"tool": tool, "arguments": arguments, "ms": duration_ms})

        self._write(work)

    def finish(self, run_id: str, result: AgentResult) -> None:
        ctx = self._contexts.pop(run_id, None)

        def work(conn: Connection) -> None:
            conn.execute(
                "UPDATE AGENT_RUNS SET STATUS = {status}, INPUT_TOKENS = {input!d}, OUTPUT_TOKENS = {output!d}, "
                "COST_USD = {cost}, RESULT_JSON = {result}, FINISHED_AT = {now} WHERE RUN_ID = {run_id}",
                {"status": result.status, "input": result.usage.input_tokens + result.usage.cache_read_tokens,
                 "output": result.usage.output_tokens, "cost": f"{result.cost_usd:.6f}",
                 "result": to_json({"output": result.output, "error": result.error, "steps": result.steps}),
                 "now": ts(utcnow()), "run_id": run_id},
            )
            if ctx is not None and ctx.incident_id:
                add_event(conn, ctx.workspace_id, ctx.incident_id, "agent.finished", f"agent:{result.agent}",
                          {"run_id": run_id, "status": result.status, "steps": result.steps,
                           "tokens": result.usage.total, "cost_usd": result.cost_usd, "error": result.error or None})

        self._write(work)
