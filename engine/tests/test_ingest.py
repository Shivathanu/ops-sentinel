import json
from datetime import datetime

import pytest

from sentinel.core.models import Severity
from sentinel.core.spec import SourceSpec, parse_spec
from sentinel.ingest.service import IngestFailure, authorize, ingest
from sentinel.ingest.sources import SourceError, SourceRegistry, parse_time
from sentinel.store.incidents import RecordedSignal

REGISTRY = SourceRegistry(load_plugins=False)

ALERTMANAGER = {
    "version": "4",
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "PipelineFailed", "severity": "critical", "service": "nightly-export"},
            "annotations": {"summary": "nightly export failed 3 times"},
            "startsAt": "2026-10-31T02:00:01.123456789Z",
            "fingerprint": "abc123",
        },
        {
            "status": "resolved",
            "labels": {"alertname": "DiskFull", "severity": "warning"},
            "annotations": {},
            "startsAt": "2026-10-31T01:00:00Z",
            "fingerprint": "def456",
        },
    ],
}


def _parse(kind: str, body: bytes | dict | list, **spec: object) -> list:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return REGISTRY.get(kind).parse(SourceSpec(name="src", type=kind, **spec), raw, {})


def test_alertmanager_alerts_become_signals_with_severity_resolution_and_time() -> None:
    firing, resolved = _parse("alertmanager", ALERTMANAGER, labels={"team": "data"})

    assert firing.summary == "PipelineFailed: nightly export failed 3 times"
    assert firing.severity is Severity.CRITICAL
    assert firing.labels == {"alertname": "PipelineFailed", "severity": "critical", "service": "nightly-export", "team": "data"}
    assert firing.observed_at == datetime(2026, 10, 31, 2, 0, 1, 123456)
    assert not firing.resolved
    assert resolved.resolved and resolved.severity is Severity.MEDIUM
    assert firing.fingerprint != resolved.fingerprint


def test_the_same_alert_always_gets_the_same_fingerprint() -> None:
    assert _parse("alertmanager", ALERTMANAGER)[0].fingerprint == _parse("grafana", ALERTMANAGER)[0].fingerprint


def test_generic_webhook_understands_common_fields_lists_and_text() -> None:
    (one,) = _parse("webhook", {"title": "CRM API down", "severity": "P2", "id": 42, "status": "firing"})
    assert (one.summary, one.severity, one.resolved) == ("CRM API down", Severity.HIGH, False)

    signals = _parse("webhook", [{"message": "a"}, {"message": "b", "status": "resolved"}])
    assert [s.resolved for s in signals] == [False, True]

    (text,) = _parse("webhook", b"backup job exited 1")
    assert text.summary == "backup job exited 1"


def test_generic_webhook_field_mapping_reaches_into_nested_payloads() -> None:
    (signal,) = _parse("webhook", {"alert": {"name": "Queue lag", "uid": "q-1", "prio": "crit"}},
                       fields={"summary": "alert.name", "key": "alert.uid", "severity": "alert.prio"})

    assert (signal.summary, signal.severity) == ("Queue lag", Severity.CRITICAL)


def test_runner_events_keep_the_v1_cli_contract() -> None:
    (signal,) = _parse("sentinel_runner", {"source": "nightly-export", "job": "node export.js",
                                           "error": "line 1\nERROR connection refused\n", "exit_code": 1,
                                           "fix_id": "rerun-export"})

    assert signal.summary == "nightly-export: node export.js failed — ERROR connection refused"
    assert signal.labels["fix_id"] == "rerun-export"
    assert signal.severity is Severity.HIGH
    assert signal.payload["exit_code"] == 1


@pytest.mark.parametrize(("kind", "body"), [("alertmanager", b"not json"), ("alertmanager", {"no": "alerts"}),
                                            ("webhook", b""), ("sentinel_runner", {"error": "x"})])
def test_bad_bodies_are_rejected_with_a_reason(kind: str, body: bytes | dict) -> None:
    with pytest.raises(SourceError):
        _parse(kind, body)


def test_time_parsing_handles_go_and_python_formats() -> None:
    assert parse_time("2026-10-31T02:00:01Z") == datetime(2026, 10, 31, 2, 0, 1)
    assert parse_time("2026-10-31T04:00:01.5+02:00") == datetime(2026, 10, 31, 2, 0, 1, 500000)
    assert parse_time("0001-01-01T00:00:00Z").year == datetime.now().year


# Service ------------------------------------------------------------------------------------------------------------

class _Store:
    def __init__(self) -> None:
        self.recorded: list = []

    def record_signal(self, workspace: str, environment: str, signal: object) -> RecordedSignal:
        self.recorded.append((workspace, environment, signal))
        return RecordedSignal("S", "I", True)


class _Platform:
    def __init__(self, spec: dict) -> None:
        self._loaded = parse_spec(spec, env={})
        self.store = _Store()
        self.sources = REGISTRY

    def workspace(self, name: str):
        if name != self._loaded.spec.workspace:
            return None
        return type("Entry", (), {"loaded": self._loaded})()


SPEC = {"workspace": "team-a", "environment": "staging",
        "sources": [{"name": "am", "type": "alertmanager", "token": "source-token-1"}, {"name": "hook", "type": "webhook"}]}


def test_ingest_records_each_signal_in_the_workspace_environment() -> None:
    platform = _Platform(SPEC)

    recorded = ingest(platform, "team-a", "am", json.dumps(ALERTMANAGER).encode(),
                      {"Authorization": "Bearer source-token-1"}, api_key="engine-key")

    assert len(recorded) == 2
    assert {(w, e) for w, e, _ in platform.store.recorded} == {("team-a", "staging")}


@pytest.mark.parametrize(("workspace", "source"), [("team-b", "am"), ("team-a", "nope")])
def test_unknown_workspace_and_source_look_the_same(workspace: str, source: str) -> None:
    with pytest.raises(IngestFailure) as failure:
        ingest(_Platform(SPEC), workspace, source, b"{}", {}, api_key="k")
    assert (failure.value.status_code, failure.value.detail) == (404, "unknown workspace or source")


def test_a_source_token_is_required_and_the_api_key_does_not_replace_it() -> None:
    source = parse_spec(SPEC, env={}).spec.source("am")

    authorize(source, {"x-sentinel-token": "source-token-1"}, api_key="engine-key", auth_disabled=False)
    with pytest.raises(IngestFailure, match="invalid or missing token"):
        authorize(source, {"x-api-key": "engine-key"}, api_key="engine-key", auth_disabled=False)
    with pytest.raises(IngestFailure, match="invalid or missing token"):
        authorize(source, {}, api_key="engine-key", auth_disabled=True)  # disabling API auth never opens a token source


def test_a_source_without_a_token_takes_the_api_key_and_fails_closed_without_one() -> None:
    source = parse_spec(SPEC, env={}).spec.source("hook")

    authorize(source, {"authorization": "Bearer engine-key"}, api_key="engine-key", auth_disabled=False)
    with pytest.raises(IngestFailure) as failure:
        authorize(source, {"authorization": "Bearer x"}, api_key="", auth_disabled=False)
    assert failure.value.status_code == 503
