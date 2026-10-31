import json

import pytest
from fastapi.testclient import TestClient

from sentinel.api import v1
from sentinel.core.models import Incident, IncidentStatus, Severity, utcnow
from sentinel.core.spec import parse_spec
from sentinel.ingest.sources import SourceRegistry
from sentinel.main import app
from sentinel.store.incidents import RecordedSignal


class _Store:
    def __init__(self) -> None:
        self.incident = Incident(incident_id="INC1", workspace_id="team-a", fingerprint="f", title="Export failed",
                                 severity=Severity.HIGH, status=IncidentStatus.OPEN, opened_at=utcnow(),
                                 updated_at=utcnow())

    def record_signal(self, workspace, environment, signal):
        return RecordedSignal("SIG1", "INC1", True)

    def list_incidents(self, workspace, status=None, limit=50):
        return [self.incident]

    def get(self, workspace, incident_id):
        return self.incident if incident_id == "INC1" else None

    def events(self, workspace, incident_id):
        return []


class _Platform:
    def __init__(self) -> None:
        self._loaded = parse_spec({"workspace": "team-a", "sources": [
            {"name": "am", "type": "alertmanager", "token": "tok-123"}]}, env={})
        self.store = _Store()
        self.sources = SourceRegistry(load_plugins=False)
        self.workspaces = type("R", (), {"names": lambda s: ["team-a"], "entries": lambda s: [
            type("E", (), {"loaded": self._loaded})()]})()

    def workspace(self, name):
        return type("E", (), {"loaded": self._loaded})() if name == "team-a" else None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    platform = _Platform()
    monkeypatch.setattr(v1, "get_platform", lambda: platform)
    monkeypatch.setattr(v1.settings, "api_key", "engine-key")
    monkeypatch.setattr("sentinel.security.settings.api_key", "engine-key")
    return TestClient(app)


def test_ingest_accepts_an_alertmanager_delivery(client: TestClient) -> None:
    body = {"alerts": [{"status": "firing", "labels": {"alertname": "X"}, "annotations": {}}]}

    response = client.post("/v1/ingest/team-a/am", content=json.dumps(body), headers={"Authorization": "Bearer tok-123"})

    assert response.status_code == 202
    assert response.json() == {"accepted": 1, "signals": [{"signal_id": "SIG1", "incident_id": "INC1", "new_incident": True}]}


def test_ingest_rejects_a_wrong_token_and_a_bad_body(client: TestClient) -> None:
    assert client.post("/v1/ingest/team-a/am", content="{}", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/v1/ingest/team-a/am", content="{}", headers={"Authorization": "Bearer tok-123"}).status_code == 400


def test_reads_need_the_api_key_and_stay_inside_the_workspace(client: TestClient) -> None:
    assert client.get("/v1/workspaces/team-a/incidents").status_code == 401

    headers = {"X-API-Key": "engine-key"}
    assert client.get("/v1/workspaces/team-a/incidents", headers=headers).json()["incidents"][0]["incident_id"] == "INC1"
    assert client.get("/v1/workspaces/team-b/incidents", headers=headers).status_code == 404
    assert client.get("/v1/workspaces/team-a/incidents/INC1", headers=headers).json()["incident"]["title"] == "Export failed"
    assert client.get("/v1/workspaces/team-a/incidents/NOPE", headers=headers).status_code == 404


def test_without_a_spec_the_platform_routes_say_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v1, "get_platform", lambda: None)

    response = TestClient(app).post("/v1/ingest/team-a/am", content="{}")

    assert response.status_code == 503 and "OPS_SENTINEL_SPEC_FILE" in response.json()["detail"]
