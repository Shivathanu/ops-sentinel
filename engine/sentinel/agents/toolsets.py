"""Built-in toolsets.

- `incident`: the current incident with its signals and timeline, past incidents, and the workspace's pre-approved
  action catalog. Always available.
- `exasol`: read-only SQL for investigating the data platform: discovery, bounded queries, and anomaly scores.
  Configured with `toolsets: {exasol: {schemas: [...]}}`. It connects as the read-only investigation user
  (EXA_USER), so the database enforces read-only even if a check here were bypassed.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from sentinel.agents.tools import Tool, ToolContext, ToolOutput, ToolRegistry, schema
from sentinel.core.spec import WorkspaceSpec
from sentinel.store.exasol import Connection
from sentinel.store.incidents import IncidentStore
from sentinel.udf_lib.anomaly import robust_zscores

MAX_ROWS = 200
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
READ_ONLY_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


def incident_toolset(store: IncidentStore, spec: WorkspaceSpec) -> list[Tool]:
    def get_incident(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        incident_id = args.get("incident_id") or ctx.incident_id
        incident = store.get(ctx.workspace_id, incident_id or "")
        if incident is None:
            return {"error": f"no incident {incident_id!r} in this workspace"}
        return {
            "incident": incident.model_dump(mode="json"),
            "signals": [{**s, "payload": _clip(s["payload"])} for s in store.signals(ctx.workspace_id, incident.incident_id, 10)],
            "timeline": [{"kind": e.kind, "actor": e.actor, "at": e.created_at} for e in
                         store.events(ctx.workspace_id, incident.incident_id)][-30:],
        }

    def search_incidents(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if not any(args.get(k) for k in ("fingerprint", "service", "text")):
            return {"error": "give at least one of fingerprint, service or text"}
        found = store.search(ctx.workspace_id, fingerprint=args.get("fingerprint"), service=args.get("service"),
                             text=args.get("text"), days=args.get("days", 30), limit=args.get("limit", 10),
                             exclude=ctx.incident_id)
        return {"incidents": [
            {**i.model_dump(mode="json", include={"incident_id", "title", "severity", "status", "service",
                                                  "signal_count", "opened_at", "resolved_at"}),
             "analysis": _clip(store.summary(ctx.workspace_id, i.incident_id), 600)}
            for i in found
        ]}

    def list_actions(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {"actions": [{"id": a.id, "description": a.description, "risk": a.risk.value, "kind": a.kind,
                             "params": a.params} for a in spec.actions]}

    return [
        Tool("get_incident", "The incident (default: the current one) with its latest signals and timeline.",
             schema({"incident_id": {"type": "string", "description": "Omit for the current incident"}}),
             get_incident, toolset="incident"),
        Tool("search_incidents", "Past incidents in this workspace matching a fingerprint, service or title text, "
                                 "with their analyses. Use it to spot repeats.",
             schema({"fingerprint": {"type": "string"}, "service": {"type": "string"},
                     "text": {"type": "string", "description": "Words from the title"},
                     "days": {"type": "integer", "description": "Look-back window, default 30"},
                     "limit": {"type": "integer", "description": "Default 10"}}),
             search_incidents, toolset="incident"),
        Tool("list_actions", "The pre-approved remediation actions this workspace allows, with their risk.",
             schema({}), list_actions, toolset="incident"),
    ]


def exasol_toolset(connect: Callable[[], AbstractContextManager[Connection]], options: dict[str, Any]) -> list[Tool]:
    allowed = [s.upper() for s in options.get("schemas", [])]
    timeout = int(options.get("query_timeout_seconds", 30))

    def check_schema(name: str) -> str | None:
        if not IDENTIFIER.match(name or ""):
            return f"'{name}' is not a plain identifier"
        if allowed and name.upper() not in allowed:
            return f"schema '{name}' is not available; allowed: {', '.join(allowed)}"
        return None

    def rows_of(conn: Connection, sql: str, params: dict[str, Any] | None = None) -> tuple[None, list[tuple]]:
        return None, conn.execute(sql, params).fetchall()

    def list_tables(args: dict[str, Any], ctx: ToolContext) -> Any:
        schema_name = args.get("schema")
        with connect() as conn:
            if schema_name:
                if problem := check_schema(schema_name):
                    return ToolOutput(problem, is_error=True)
                _, rows = rows_of(conn, "SELECT TABLE_NAME, TABLE_ROW_COUNT, TABLE_COMMENT FROM EXA_ALL_TABLES "
                                        "WHERE TABLE_SCHEMA = {s} ORDER BY TABLE_NAME", {"s": schema_name.upper()})
                return {"schema": schema_name.upper(),
                        "tables": [{"table": r[0], "rows": r[1], "comment": r[2]} for r in rows]}
            _, rows = rows_of(conn, "SELECT TABLE_SCHEMA, COUNT(*) FROM EXA_ALL_TABLES GROUP BY TABLE_SCHEMA ORDER BY 1")
            return {"schemas": [{"schema": r[0], "tables": r[1]} for r in rows if not allowed or r[0] in allowed]}

    def describe_table(args: dict[str, Any], ctx: ToolContext) -> Any:
        if (problem := check_schema(args["schema"])) or not IDENTIFIER.match(args["table"]):
            return ToolOutput(problem or f"'{args['table']}' is not a plain identifier", is_error=True)
        with connect() as conn:
            _, rows = rows_of(conn, "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_IS_NULLABLE, COLUMN_COMMENT FROM "
                                    "EXA_ALL_COLUMNS WHERE COLUMN_SCHEMA = {s} AND COLUMN_TABLE = {t} "
                                    "ORDER BY COLUMN_ORDINAL_POSITION",
                              {"s": args["schema"].upper(), "t": args["table"].upper()})
        if not rows:
            return ToolOutput(f"no table {args['schema']}.{args['table']}", is_error=True)
        return {"columns": [{"name": r[0], "type": r[1], "nullable": r[2], "comment": r[3]} for r in rows]}

    def query(args: dict[str, Any], ctx: ToolContext) -> Any:
        sql = args["sql"].strip().rstrip(";").strip()
        if not READ_ONLY_START.match(sql) or ";" in sql:
            return ToolOutput("only a single SELECT or WITH statement is allowed", is_error=True)
        limit = max(1, min(int(args.get("max_rows", 50)), MAX_ROWS))
        with connect() as conn:
            conn.execute(f"ALTER SESSION SET QUERY_TIMEOUT = {timeout}")
            statement = conn.execute(f"SELECT * FROM ({sql}) LIMIT {limit + 1}")
            rows = statement.fetchall()
            columns = list(statement.column_names()) if hasattr(statement, "column_names") else []
        truncated = len(rows) > limit
        return {"columns": columns, "rows": [list(r) for r in rows[:limit]], "row_count": min(len(rows), limit),
                "truncated": truncated}

    def metric_anomalies(args: dict[str, Any], ctx: ToolContext) -> Any:
        names = [args["schema"], args["table"], args["series_column"], args["time_column"], args["value_column"]]
        if problem := check_schema(args["schema"]):
            return ToolOutput(problem, is_error=True)
        if not all(IDENTIFIER.match(n) for n in names):
            return ToolOutput("schema, table and column names must be plain identifiers", is_error=True)
        s, t, series, when, value = (n.upper() for n in names)
        hours = max(1, min(int(args.get("since_hours", 168)), 24 * 90))
        with connect() as conn:
            conn.execute(f"ALTER SESSION SET QUERY_TIMEOUT = {timeout}")
            rows = conn.execute(
                f'SELECT "{series}", "{when}", CAST("{value}" AS DOUBLE) FROM "{s}"."{t}" '
                f'WHERE "{when}" >= ADD_HOURS(CURRENT_TIMESTAMP, -{hours}) AND "{value}" IS NOT NULL '
                f'ORDER BY "{series}", "{when}" LIMIT 20000'
            ).fetchall()
        by_series: dict[str, list[tuple]] = {}
        for key, at, val in rows:
            by_series.setdefault(str(key), []).append((at, val))
        flagged = []
        for key, points in by_series.items():
            for (at, val), score in zip(points, robust_zscores([p[1] for p in points]), strict=True):
                if score is not None and abs(score) >= float(args.get("threshold", 3.5)):
                    flagged.append({"series": key, "at": at, "value": val, "zscore": round(score, 2)})
        flagged.sort(key=lambda f: -abs(f["zscore"]))
        return {"series_checked": len(by_series), "points_checked": len(rows), "anomalies": flagged[:25],
                "method": "robust z-score (median/MAD)"}

    ident = {"type": "string"}
    return [
        Tool("exasol_list_tables", "List schemas, or the tables of one schema with row counts.",
             schema({"schema": {"type": "string", "description": "Omit to list schemas"}}), list_tables, toolset="exasol"),
        Tool("exasol_describe_table", "Columns and types of a table.",
             schema({"schema": ident, "table": ident}, ["schema", "table"]), describe_table, toolset="exasol"),
        Tool("exasol_query", "Run one read-only SELECT/WITH query. Results are capped; filter and aggregate in SQL.",
             schema({"sql": {"type": "string"}, "max_rows": {"type": "integer", "description": f"Default 50, max {MAX_ROWS}"}},
                    ["sql"]), query, toolset="exasol", max_output_chars=15_000),
        Tool("exasol_metric_anomalies", "Find outliers in a metric per series (e.g. pipeline duration per pipeline, "
                                        "cost per user) with a robust z-score.",
             schema({"schema": ident, "table": ident, "series_column": ident, "time_column": ident,
                     "value_column": ident, "since_hours": {"type": "integer", "description": "Default 168"},
                     "threshold": {"type": "number", "description": "Default 3.5"}},
                    ["schema", "table", "series_column", "time_column", "value_column"]),
             metric_anomalies, toolset="exasol"),
    ]


def build_registry(store: IncidentStore, spec: WorkspaceSpec,
                   exasol_connect: Callable[[], AbstractContextManager[Connection]] | None) -> ToolRegistry:
    registry = ToolRegistry().add(*incident_toolset(store, spec))
    if "exasol" in spec.toolsets and exasol_connect is not None:
        registry.add(*exasol_toolset(exasol_connect, spec.toolsets["exasol"]))
    return registry


def _clip(value: Any, limit: int = 1500) -> Any:
    text = str(value)
    return value if len(text) <= limit else text[:limit] + "…"
