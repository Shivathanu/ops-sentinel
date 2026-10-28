"""The agent-facing endpoints and the incident lifecycle behind them, run against scripted fakes."""
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from ops_sentinel import agent_actions, agent_events, main

API_KEY = "test-api-key"
HEADERS = {"X-API-Key": API_KEY}
EVENT = {"source": "nightly-export", "job": "export.sh", "error": "exit 2: disk full", "exit_code": 2, "fix_id": "rerun-export"}

client = TestClient(main.app)


class FakeResult:
    def __init__(self, rows: list[tuple] | None = None, rowcount: int = 1) -> None:
        self._rows, self._rowcount = rows or [], rowcount

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def rowcount(self) -> int:
        return self._rowcount


class FakeConnection:
    def __init__(self, routes: list[tuple[str, FakeResult]]) -> None:
        self._routes = routes
        self.executed: list[tuple[str, dict | None]] = []

    def execute(self, sql: str, params: dict | None = None) -> FakeResult:
        self.executed.append((sql, params))
        for needle, result in self._routes:
            if needle in sql:
                return result
        return FakeResult()


def _wire_connection(monkeypatch: pytest.MonkeyPatch, conn: FakeConnection) -> None:
    @contextmanager
    def connection() -> Iterator[FakeConnection]:
        yield conn

    monkeypatch.setattr(agent_events, "get_connection", connection)


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.settings, "api_key", API_KEY)
    monkeypatch.setattr(main.settings, "api_auth_disabled", False)


def test_event_endpoints_require_the_api_key() -> None:
    assert client.post("/api/events", json=EVENT).status_code == 401
    assert client.get("/api/events/1").status_code == 401
    assert client.post("/api/events/1/result", json={}).status_code == 401


def test_reporting_a_failure_returns_the_incident_id_and_schedules_triage(monkeypatch: pytest.MonkeyPatch) -> None:
    triaged: list[int] = []

    async def fake_triage(incident_id: int, event: agent_events.FailureEvent) -> None:
        triaged.append(incident_id)

    monkeypatch.setattr(agent_events, "open_incident", lambda event: 41)
    monkeypatch.setattr(agent_events, "triage_and_notify", fake_triage)

    response = client.post("/api/events", json=EVENT, headers=HEADERS)

    assert response.status_code == 202
    assert response.json() == {"incident_id": 41}
    assert triaged == [41]


@pytest.mark.parametrize(
    "bad",
    [
        {**EVENT, "fix_id": "rm -rf /"},
        {**EVENT, "fix_id": "a;b"},
        {**EVENT, "source": ""},
        {**EVENT, "error": "x" * 4001},
    ],
)
def test_malformed_events_are_rejected(bad: dict) -> None:
    assert client.post("/api/events", json=bad, headers=HEADERS).status_code == 422


def test_status_of_an_unknown_or_non_agent_incident_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_connection(monkeypatch, FakeConnection([("SELECT STATUS", FakeResult())]))

    assert client.get("/api/events/5", headers=HEADERS).status_code == 404


def test_status_reports_the_fix_id_only(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = agent_actions.format_action("rerun-export", "nightly-export", "export.sh")
    _wire_connection(monkeypatch, FakeConnection([("SELECT STATUS", FakeResult([("APPROVED", marker)]))]))

    response = client.get("/api/events/5", headers=HEADERS)

    assert response.json() == {"incident_id": 5, "status": "APPROVED", "fix_id": "rerun-export"}


def _result(outcome: str = "SUCCESS") -> agent_events.EventResult:
    return agent_events.EventResult(agent="nightly-export", outcome=outcome, detail="exit 0", attempts=1)


def test_a_result_closes_an_approved_agent_incident_and_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConnection([
        ("UPDATE INCIDENTS SET STATUS", FakeResult(rowcount=1)),
        ("SELECT SUGGESTED_FIX_SQL, SLACK_MESSAGE_TS", FakeResult([("ACTION: rerun-export (x)", "1.2")])),
    ])
    _wire_connection(monkeypatch, conn)
    cards: list[str] = []
    monkeypatch.setattr(agent_events, "update_incident_card", lambda ts, text: cards.append(text))

    assert agent_events.record_result(5, _result()) is True

    update_sql, update_params = conn.executed[0]
    assert "AND STATUS = 'APPROVED'" in update_sql
    assert update_params["status"] == "REMEDIATED" and update_params["source_type"] == "CLI_EVENT"
    audit = next(p for sql, p in conn.executed if sql.startswith("INSERT INTO REMEDIATION_ACTIONS"))
    assert (audit["outcome"], audit["executed_by"]) == ("SUCCESS", "agent:nightly-export")
    assert "fix applied" in cards[0]


def test_a_failed_result_marks_the_incident_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConnection([
        ("UPDATE INCIDENTS SET STATUS", FakeResult(rowcount=1)),
        ("SELECT SUGGESTED_FIX_SQL, SLACK_MESSAGE_TS", FakeResult([("ACTION: rerun-export (x)", None)])),
    ])
    _wire_connection(monkeypatch, conn)

    assert agent_events.record_result(5, _result("FAILED")) is True

    assert conn.executed[0][1]["status"] == "FAILED" and conn.executed[0][1]["resolved_ts"] is None


def test_a_result_for_an_incident_that_is_not_approved_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConnection([("UPDATE INCIDENTS SET STATUS", FakeResult(rowcount=0))])
    _wire_connection(monkeypatch, conn)

    assert agent_events.record_result(5, _result()) is False
    assert not any(sql.startswith("INSERT") for sql, _ in conn.executed)


def test_the_result_endpoint_answers_409_when_nothing_was_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_events, "record_result", lambda incident_id, result: False)
    body = {"agent": "a", "outcome": "SUCCESS", "detail": "ok", "attempts": 1}

    assert client.post("/api/events/5/result", json=body, headers=HEADERS).status_code == 409


def test_a_slack_outage_does_not_fail_the_recorded_result(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConnection([
        ("UPDATE INCIDENTS SET STATUS", FakeResult(rowcount=1)),
        ("SELECT SUGGESTED_FIX_SQL, SLACK_MESSAGE_TS", FakeResult([("ACTION: rerun-export (x)", "1.2")])),
    ])
    _wire_connection(monkeypatch, conn)

    def broken(ts: str, text: str) -> None:
        raise RuntimeError("slack is down")

    monkeypatch.setattr(agent_events, "update_incident_card", broken)

    assert agent_events.record_result(5, _result()) is True


def test_triage_always_proposes_the_configured_fix_never_a_model_suggested_one(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: list[dict] = []
    posted: list[dict] = []
    monkeypatch.setattr(
        agent_events, "triage",
        lambda ctx, lineage: {"business_impact": "i", "root_cause": "r", "suggested_fix_sql": "DROP TABLE x", "risk_level": "LOW"},
    )
    monkeypatch.setattr(agent_events, "update_incident_triage", lambda incident_id, result: stored.append(result))
    monkeypatch.setattr(agent_events, "post_incident_card", lambda **kw: posted.append(kw) or "1.2")
    monkeypatch.setattr(agent_events, "store_slack_ts", lambda incident_id, ts: None)

    asyncio.run(agent_events.triage_and_notify(3, agent_events.FailureEvent(**EVENT)))

    assert stored[0]["suggested_fix_sql"].startswith("ACTION: rerun-export")
    assert posted[0]["suggested_fix_sql"].startswith("ACTION: rerun-export")
    assert "DROP" not in posted[0]["suggested_fix_sql"]


def test_a_failed_triage_still_announces_the_raw_error_as_high_risk(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict] = []

    def broken(ctx: dict, lineage: list) -> dict:
        raise ValueError("model returned bad JSON")

    monkeypatch.setattr(agent_events, "triage", broken)
    monkeypatch.setattr(agent_events, "update_incident_triage", lambda incident_id, result: None)
    monkeypatch.setattr(agent_events, "post_incident_card", lambda **kw: posted.append(kw) or "1.2")
    monkeypatch.setattr(agent_events, "store_slack_ts", lambda incident_id, ts: None)

    asyncio.run(agent_events.triage_and_notify(3, agent_events.FailureEvent(**{**EVENT, "fix_id": None})))

    assert posted[0]["risk_level"] == "HIGH"
    assert "disk full" in posted[0]["root_cause"]
    assert posted[0]["suggested_fix_sql"] is None


def test_the_marker_round_trips_and_rejects_everything_else() -> None:
    marker = agent_actions.format_action("rerun-export", "src", "job")

    assert agent_actions.parse_fix_id(marker) == "rerun-export"
    assert agent_actions.parse_fix_id("UPDATE t SET a = 1 WHERE b = 2") is None
    assert agent_actions.parse_fix_id("ACTION: bad id with spaces") is None
    assert agent_actions.parse_fix_id(None) is None
