"""
The READ path — this is Ops Sentinel acting as its own MCP *client*, the same
way Cursor/Claude/Codex do against the exakit-managed `exasol` MCP server.
It only ever calls read tools (list tables, describe, run a SELECT) against
the dedicated read-only MCP login, so a bug here can never corrupt data —
the database enforces that, not this code.

This module intentionally does NOT expose a way to run DDL/DML. Remediation
writes go through ops_sentinel/db.py + ops_sentinel/remediation.py instead, using a
separate admin connection, after a human approves in Slack.
"""
import json
import os
import shlex
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ops_sentinel.config import settings


@asynccontextmanager
async def mcp_session():
    # Mirrors exactly what `exakit mcp-setup` writes into Claude/Codex/
    # Cursor's own MCP config for the `exasol` server — same command, same
    # env vars — so this is the real read-only MCP connection, not a
    # reimplementation of it. Merge with the current environment (not
    # replace it) so the subprocess can still find `uv`'s cache, PATH, etc.
    params = StdioServerParameters(
        command=settings.exasol_mcp_command,
        args=shlex.split(settings.exasol_mcp_args),
        env={
            **os.environ,
            "EXA_DSN": settings.exa_dsn,
            "EXA_USER": settings.exa_user,
            "EXA_PASSWORD": settings.exa_password,
            "EXA_MCP_SETTINGS": settings.exa_mcp_settings,
            "EXA_SSL_CERT_VALIDATION": settings.exa_ssl_cert_validation,
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def run_readonly_sql(sql: str) -> list[dict]:
    """Executes a single SELECT through the MCP server's SQL tool.

    The MCP login is database-enforced read-only, so anything other than a
    SELECT is rejected by Exasol itself, not by this function — but we still
    refuse obviously non-SELECT input early with a clear error for a better
    demo experience.
    """
    if not sql.strip().lower().startswith("select"):
        raise ValueError("mcp_client.run_readonly_sql only accepts SELECT statements")

    async with mcp_session() as session:
        tools = await session.list_tools()
        tool_name = _find_sql_tool_name(tools.tools)
        result = await session.call_tool(tool_name, arguments={"query": sql})
        return _parse_tool_result(result)


async def get_error_context(pipeline_id: str, log_id: int) -> dict:
    """Step 2-3 of the loop: pull the failing run's own row for context."""
    sql = (
        "SELECT LOG_ID, PIPELINE_ID, PIPELINE_NAME, SOURCE_TABLE, TARGET_TABLE, "
        "EXECUTION_TS, STATUS, ERROR_STACK "
        f"FROM SENTINEL.PIPELINE_LOGS WHERE LOG_ID = {int(log_id)}"
    )
    rows = await run_readonly_sql(sql)
    return rows[0] if rows else {}


async def get_lineage_for_table(table_name: str) -> list[dict]:
    """Trace downstream impact so the LLM can describe business impact."""
    sql = (
        "SELECT SOURCE_TABLE, TARGET_TABLE, DOWNSTREAM_DASHBOARD, OWNER_TEAM, CRITICALITY "
        f"FROM SENTINEL.DATA_LINEAGE WHERE SOURCE_TABLE = '{table_name}' "
        f"OR TARGET_TABLE = '{table_name}'"
    )
    return await run_readonly_sql(sql)


def _find_sql_tool_name(tools) -> str:
    """The exakit MCP server exposes a query-running tool; names can shift
    between versions, so pick the first tool that looks like it runs SQL
    rather than hardcoding a name that might change under you mid-hackathon.
    """
    for tool in tools:
        name = tool.name.lower()
        if "query" in name or "sql" in name or "execute" in name:
            return tool.name
    raise RuntimeError(
        "No SQL-execution tool found on the Exasol MCP server. "
        "Run `exakit mcp-doctor` to confirm the server is healthy."
    )


def _parse_tool_result(result) -> list[dict]:
    """MCP tool results come back as content blocks; the Exasol MCP server
    returns structured JSON text for query results. Fall back gracefully if
    the shape differs across versions.
    """
    rows: list[dict] = []
    for block in result.content:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            rows.extend(parsed)
        elif isinstance(parsed, dict) and "rows" in parsed:
            rows.extend(parsed["rows"])
    return rows
