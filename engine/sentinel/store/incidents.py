"""Signals, incidents, their timeline and the audit log. Every query is scoped by workspace.

Exasol has no row-level security, so tenancy is enforced here: no method reads or writes a row without the caller's
workspace id in its WHERE clause. Routes and handlers reach the tables only through this module.
"""
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sentinel.core.models import (
    CLOSED_STATUSES,
    Incident,
    IncidentStatus,
    Severity,
    Signal,
    new_id,
    utcnow,
)
from sentinel.store.exasol import (
    Connection,
    ConnectionFactory,
    from_json,
    in_list,
    run_in_transaction,
    to_json,
    ts,
)
from sentinel.store.queue import enqueue_in

logger = logging.getLogger("sentinel.incidents")

INCIDENT_OPENED = "incident.opened"
INCIDENT_SIGNAL = "incident.signal"
_CLOSED = sorted(status.value for status in CLOSED_STATUSES)
_COLUMNS = (
    "INCIDENT_ID, WORKSPACE_ID, FINGERPRINT, TITLE, SEVERITY, STATUS, SERVICE, SOURCE_NAME, LABELS, SIGNAL_COUNT, "
    "OPENED_AT, UPDATED_AT, RESOLVED_AT"
)


@dataclass(frozen=True)
class RecordedSignal:
    signal_id: str
    incident_id: str | None
    created: bool  # True when this signal opened a new incident


@dataclass(frozen=True)
class IncidentEvent:
    event_id: str
    kind: str
    actor: str
    detail: Any
    created_at: datetime


def _incident(row: Sequence[Any]) -> Incident:
    (incident_id, workspace_id, fingerprint, title, severity, status, service, source, labels, count,
     opened, updated, resolved) = row
    return Incident(
        incident_id=incident_id,
        workspace_id=workspace_id,
        fingerprint=fingerprint,
        title=title,
        severity=Severity(severity),
        status=IncidentStatus(status),
        service=service,
        source=source,
        labels=from_json(labels, {}),
        signal_count=int(count),
        opened_at=opened,
        updated_at=updated,
        resolved_at=resolved,
    )


def add_event(conn: Connection, workspace_id: str, incident_id: str, kind: str, actor: str, detail: Any = None) -> str:
    event_id = new_id()
    conn.execute(
        "INSERT INTO INCIDENT_EVENTS (EVENT_ID, INCIDENT_ID, WORKSPACE_ID, KIND, ACTOR, DETAIL, CREATED_AT) "
        "VALUES ({event_id}, {incident_id}, {workspace_id}, {kind}, {actor}, {detail}, {created_at})",
        {
            "event_id": event_id,
            "incident_id": incident_id,
            "workspace_id": workspace_id,
            "kind": kind,
            "actor": actor,
            "detail": None if detail is None else to_json(detail),
            "created_at": ts(utcnow()),
        },
    )
    return event_id


def audit(
    conn: Connection,
    *,
    actor: str,
    action: str,
    workspace_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    detail: Any = None,
) -> None:
    conn.execute(
        "INSERT INTO AUDIT_EVENTS (AUDIT_ID, WORKSPACE_ID, ACTOR, ACTION_NAME, SUBJECT_TYPE, SUBJECT_ID, DETAIL, CREATED_AT) "
        "VALUES ({audit_id}, {workspace_id}, {actor}, {action}, {subject_type}, {subject_id}, {detail}, {created_at})",
        {
            "audit_id": new_id(),
            "workspace_id": workspace_id,
            "actor": actor,
            "action": action,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "detail": None if detail is None else to_json(detail),
            "created_at": ts(utcnow()),
        },
    )


class IncidentStore:
    def __init__(self, connect: ConnectionFactory):
        self._connect = connect

    # Workspaces and config ----------------------------------------------------------------------------------------

    def ensure_workspace(self, workspace_id: str, display_name: str | None = None) -> None:
        def work(conn: Connection) -> None:
            conn.execute(
                "INSERT INTO WORKSPACES (WORKSPACE_ID, DISPLAY_NAME, CREATED_AT) "
                "SELECT {workspace_id}, {display_name}, {created_at} FROM DUAL "
                "WHERE NOT EXISTS (SELECT 1 FROM WORKSPACES WHERE WORKSPACE_ID = {workspace_id})",
                {"workspace_id": workspace_id, "display_name": display_name or workspace_id, "created_at": ts(utcnow())},
            )

        run_in_transaction(self._connect, work)

    def record_config(self, workspace_id: str, spec_text: str, applied_by: str) -> bool:
        """Stores the spec when it differs from the last applied version. Returns True when a new version was stored."""
        checksum = hashlib.sha256(spec_text.encode("utf-8")).hexdigest()

        def work(conn: Connection) -> bool:
            last = conn.execute(
                "SELECT CHECKSUM FROM CONFIG_VERSIONS WHERE WORKSPACE_ID = {workspace_id} "
                "ORDER BY APPLIED_AT DESC, CONFIG_ID DESC LIMIT 1",
                {"workspace_id": workspace_id},
            ).fetchone()
            if last is not None and last[0] == checksum:
                return False
            conn.execute(
                "INSERT INTO CONFIG_VERSIONS (CONFIG_ID, WORKSPACE_ID, CHECKSUM, SPEC_YAML, APPLIED_BY, APPLIED_AT) "
                "VALUES ({config_id}, {workspace_id}, {checksum}, {spec}, {applied_by}, {applied_at})",
                {
                    "config_id": new_id(),
                    "workspace_id": workspace_id,
                    "checksum": checksum,
                    "spec": spec_text,
                    "applied_by": applied_by,
                    "applied_at": ts(utcnow()),
                },
            )
            audit(conn, actor=applied_by, action="config.applied", workspace_id=workspace_id,
                  subject_type="config", subject_id=checksum[:16])
            return True

        return run_in_transaction(self._connect, work)

    # Signals → incidents -------------------------------------------------------------------------------------------

    def record_signal(self, workspace_id: str, environment: str, signal: Signal) -> RecordedSignal:
        """Stores the signal and correlates it: it joins the open incident with its fingerprint or opens a new one.

        Opening an incident enqueues `incident.opened` in the same transaction, so an incident can never exist
        without the job that starts its triage, and a retried ingest can never produce two jobs.
        """

        def work(conn: Connection) -> RecordedSignal:
            now = utcnow()
            signal_id = new_id()
            closed_sql, closed_params = in_list("closed", _CLOSED)
            open_row = conn.execute(
                "SELECT INCIDENT_ID, SEVERITY, ROW_VERSION FROM INCIDENTS "
                "WHERE WORKSPACE_ID = {workspace_id} AND FINGERPRINT = {fingerprint} "
                f"AND STATUS NOT IN ({closed_sql}) ORDER BY OPENED_AT DESC LIMIT 1",
                {"workspace_id": workspace_id, "fingerprint": signal.fingerprint, **closed_params},
            ).fetchone()

            incident_id: str | None
            created = False
            if open_row is not None:
                incident_id, severity, version = open_row
                worst = max(Severity(severity), signal.severity, key=lambda s: s.rank)
                conn.execute(
                    "UPDATE INCIDENTS SET SIGNAL_COUNT = SIGNAL_COUNT + 1, SEVERITY = {severity}, "
                    "UPDATED_AT = {now}, ROW_VERSION = ROW_VERSION + 1 "
                    "WHERE INCIDENT_ID = {incident_id} AND WORKSPACE_ID = {workspace_id}",
                    {"severity": worst.value, "now": ts(now), "incident_id": incident_id, "workspace_id": workspace_id},
                )
                kind = "source_resolved" if signal.resolved else "signal"
                add_event(conn, workspace_id, incident_id, kind, f"source:{signal.source}",
                          {"signal_id": signal_id, "summary": signal.summary, "severity": signal.severity.value})
                enqueue_in(conn, INCIDENT_SIGNAL, {"incident_id": incident_id, "signal_id": signal_id,
                                                   "resolved": signal.resolved},
                           workspace_id=workspace_id, idempotency_key=f"signal:{signal_id}", now=now)
            elif signal.resolved:
                incident_id = None  # a recovery for something we never saw open: keep the signal, open nothing
            else:
                incident_id = new_id()
                created = True
                conn.execute(
                    "INSERT INTO INCIDENTS (INCIDENT_ID, WORKSPACE_ID, ENVIRONMENT, FINGERPRINT, TITLE, SEVERITY, "
                    "STATUS, SERVICE, SOURCE_NAME, LABELS, SIGNAL_COUNT, OPENED_AT, UPDATED_AT, ROW_VERSION) VALUES "
                    "({incident_id}, {workspace_id}, {environment}, {fingerprint}, {title}, {severity}, 'open', "
                    "{service}, {source}, {labels}, 1, {now}, {now}, 1)",
                    {
                        "incident_id": incident_id,
                        "workspace_id": workspace_id,
                        "environment": environment,
                        "fingerprint": signal.fingerprint,
                        "title": signal.summary[:500] or f"{signal.source} alert",
                        "severity": signal.severity.value,
                        "service": signal.labels.get("service") or signal.labels.get("job"),
                        "source": signal.source,
                        "labels": to_json(signal.labels),
                        "now": ts(now),
                    },
                )
                add_event(conn, workspace_id, incident_id, "opened", f"source:{signal.source}",
                          {"signal_id": signal_id, "severity": signal.severity.value})
                enqueue_in(conn, INCIDENT_OPENED, {"incident_id": incident_id},
                           workspace_id=workspace_id, idempotency_key=f"opened:{incident_id}", now=now)

            conn.execute(
                "INSERT INTO SIGNALS (SIGNAL_ID, WORKSPACE_ID, INCIDENT_ID, SOURCE_NAME, FINGERPRINT, SEVERITY, SUMMARY, "
                "LABELS, PAYLOAD, IS_RESOLVED, OBSERVED_AT, RECEIVED_AT) VALUES ({signal_id}, {workspace_id}, "
                "{incident_id}, {source}, {fingerprint}, {severity}, {summary}, {labels}, {payload}, "
                "{resolved!r}, {observed_at}, {received_at})",
                {
                    "signal_id": signal_id,
                    "workspace_id": workspace_id,
                    "incident_id": incident_id,
                    "source": signal.source,
                    "fingerprint": signal.fingerprint,
                    "severity": signal.severity.value,
                    "summary": signal.summary,
                    "labels": to_json(signal.labels),
                    "payload": to_json(signal.payload),
                    "resolved": "TRUE" if signal.resolved else "FALSE",
                    "observed_at": ts(signal.observed_at),
                    "received_at": ts(now),
                },
            )
            return RecordedSignal(signal_id, incident_id, created)

        return run_in_transaction(self._connect, work)

    # Reads ---------------------------------------------------------------------------------------------------------

    def get(self, workspace_id: str, incident_id: str) -> Incident | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM INCIDENTS WHERE WORKSPACE_ID = {{workspace_id}} AND INCIDENT_ID = {{incident_id}}",
                {"workspace_id": workspace_id, "incident_id": incident_id},
            ).fetchone()
        return None if row is None else _incident(row)

    def list_incidents(
        self, workspace_id: str, *, status: IncidentStatus | None = None, limit: int = 50
    ) -> list[Incident]:
        params: dict[str, Any] = {"workspace_id": workspace_id, "limit": max(1, min(limit, 500))}
        where = "WORKSPACE_ID = {workspace_id}"
        if status is not None:
            where += " AND STATUS = {status}"
            params["status"] = status.value
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM INCIDENTS WHERE {where} ORDER BY OPENED_AT DESC LIMIT {{limit!d}}", params
            ).fetchall()
        return [_incident(row) for row in rows]

    def signals(self, workspace_id: str, incident_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT SIGNAL_ID, SOURCE_NAME, SEVERITY, SUMMARY, LABELS, PAYLOAD, IS_RESOLVED, OBSERVED_AT "
                "FROM SIGNALS WHERE WORKSPACE_ID = {workspace_id} AND INCIDENT_ID = {incident_id} "
                "ORDER BY SIGNAL_ID DESC LIMIT {limit!d}",
                {"workspace_id": workspace_id, "incident_id": incident_id, "limit": max(1, min(limit, 200))},
            ).fetchall()
        return [
            {"signal_id": r[0], "source": r[1], "severity": r[2], "summary": r[3], "labels": from_json(r[4], {}),
             "payload": from_json(r[5], {}), "resolved": bool(r[6]), "observed_at": r[7]}
            for r in rows
        ]

    def search(
        self,
        workspace_id: str,
        *,
        fingerprint: str | None = None,
        service: str | None = None,
        text: str | None = None,
        days: int = 30,
        limit: int = 10,
        exclude: str | None = None,
    ) -> list[Incident]:
        """Past incidents in the workspace matching any of the given criteria, newest first."""
        clauses, params = [], {"workspace_id": workspace_id, "limit": max(1, min(limit, 50)),
                               "since": ts(utcnow() - timedelta(days=max(1, min(days, 365))))}
        if fingerprint:
            clauses.append("FINGERPRINT = {fingerprint}")
            params["fingerprint"] = fingerprint
        if service:
            clauses.append("SERVICE = {service}")
            params["service"] = service
        if text:
            clauses.append("LOWER(TITLE) LIKE {pattern}")
            params["pattern"] = "%" + text.lower().replace("%", "").replace("_", "") + "%"
        where = "WORKSPACE_ID = {workspace_id} AND OPENED_AT >= {since}"
        if clauses:
            where += " AND (" + " OR ".join(clauses) + ")"
        if exclude:
            where += " AND INCIDENT_ID <> {exclude}"
            params["exclude"] = exclude
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM INCIDENTS WHERE {where} ORDER BY OPENED_AT DESC LIMIT {{limit!d}}", params
            ).fetchall()
        return [_incident(row) for row in rows]

    def summary(self, workspace_id: str, incident_id: str) -> Any:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT SUMMARY FROM INCIDENTS WHERE WORKSPACE_ID = {workspace_id} AND INCIDENT_ID = {incident_id}",
                {"workspace_id": workspace_id, "incident_id": incident_id},
            ).fetchone()
        return from_json(row[0]) if row else None

    def set_summary(self, workspace_id: str, incident_id: str, summary: dict[str, Any], actor: str) -> None:
        """Stores the latest analysis on the incident (as JSON) and adds it to the timeline in one commit."""
        def work(conn: Connection) -> None:
            conn.execute(
                "UPDATE INCIDENTS SET SUMMARY = {summary}, UPDATED_AT = {now}, ROW_VERSION = ROW_VERSION + 1 "
                "WHERE WORKSPACE_ID = {workspace_id} AND INCIDENT_ID = {incident_id}",
                {"summary": to_json(summary)[:20000], "now": ts(utcnow()), "workspace_id": workspace_id,
                 "incident_id": incident_id},
            )
            add_event(conn, workspace_id, incident_id, "analysis", actor, summary)

        run_in_transaction(self._connect, work)

    def events(self, workspace_id: str, incident_id: str) -> list[IncidentEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT EVENT_ID, KIND, ACTOR, DETAIL, CREATED_AT FROM INCIDENT_EVENTS "
                "WHERE WORKSPACE_ID = {workspace_id} AND INCIDENT_ID = {incident_id} ORDER BY EVENT_ID",
                {"workspace_id": workspace_id, "incident_id": incident_id},
            ).fetchall()
        return [IncidentEvent(r[0], r[1], r[2], from_json(r[3]), r[4]) for r in rows]

    # Writes --------------------------------------------------------------------------------------------------------

    def transition(
        self,
        workspace_id: str,
        incident_id: str,
        *,
        to: IncidentStatus,
        allowed_from: Sequence[IncidentStatus],
        actor: str,
        detail: Any = None,
    ) -> bool:
        """Moves an incident only if it is in one of `allowed_from`. The check is part of the UPDATE, so two workers
        racing to make the same move cannot both succeed."""

        def work(conn: Connection) -> bool:
            from_sql, from_params = in_list("from", [s.value for s in allowed_from])
            now = utcnow()
            moved = conn.execute(
                "UPDATE INCIDENTS SET STATUS = {to}, UPDATED_AT = {now}, ROW_VERSION = ROW_VERSION + 1, "
                "RESOLVED_AT = CASE WHEN {to} = 'resolved' THEN {now} ELSE RESOLVED_AT END "
                f"WHERE WORKSPACE_ID = {{workspace_id}} AND INCIDENT_ID = {{incident_id}} AND STATUS IN ({from_sql})",
                {"to": to.value, "now": ts(now), "workspace_id": workspace_id, "incident_id": incident_id, **from_params},
            ).rowcount()
            if not moved:
                return False
            add_event(conn, workspace_id, incident_id, f"status.{to.value}", actor, detail)
            audit(conn, actor=actor, action=f"incident.{to.value}", workspace_id=workspace_id,
                  subject_type="incident", subject_id=incident_id, detail=detail)
            return True

        return run_in_transaction(self._connect, work)

    def add_event(self, workspace_id: str, incident_id: str, kind: str, actor: str, detail: Any = None) -> str:
        return run_in_transaction(
            self._connect, lambda conn: add_event(conn, workspace_id, incident_id, kind, actor, detail)
        )

    def record_notification(
        self, workspace_id: str, incident_id: str, channel: str, purpose: str, external_ref: str | None
    ) -> None:
        def work(conn: Connection) -> None:
            conn.execute(
                "INSERT INTO NOTIFICATIONS (NOTIFICATION_ID, INCIDENT_ID, WORKSPACE_ID, CHANNEL, EXTERNAL_REF, PURPOSE, "
                "CREATED_AT) VALUES ({id}, {incident_id}, {workspace_id}, {channel}, {ref}, {purpose}, {now})",
                {
                    "id": new_id(),
                    "incident_id": incident_id,
                    "workspace_id": workspace_id,
                    "channel": channel,
                    "ref": external_ref,
                    "purpose": purpose,
                    "now": ts(utcnow()),
                },
            )
            add_event(conn, workspace_id, incident_id, "notified", f"channel:{channel}",
                      {"purpose": purpose, "ref": external_ref})

        run_in_transaction(self._connect, work)

    def notification_refs(self, workspace_id: str, incident_id: str) -> dict[str, str | None]:
        """Channel name → reference of the first message sent there, for idempotent sends and in-place updates."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT CHANNEL, EXTERNAL_REF FROM NOTIFICATIONS WHERE WORKSPACE_ID = {workspace_id} "
                "AND INCIDENT_ID = {incident_id} AND PURPOSE <> 'update' ORDER BY NOTIFICATION_ID",
                {"workspace_id": workspace_id, "incident_id": incident_id},
            ).fetchall()
        refs: dict[str, str | None] = {}
        for channel, ref in rows:
            refs.setdefault(channel, ref)
        return refs
