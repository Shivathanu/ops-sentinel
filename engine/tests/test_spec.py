from pathlib import Path

import pytest

from sentinel.core import secrets
from sentinel.core.models import Incident, IncidentStatus, Severity, utcnow
from sentinel.core.spec import SpecError, parse_spec
from sentinel.core.workspaces import WorkspaceRegistry

FULL = {
    "workspace": "data-platform",
    "sources": [
        {"name": "prometheus", "type": "alertmanager", "token": "${env:AM_TOKEN}"},
        {"name": "etl-log", "type": "logfile", "runner": "etl-box", "path": "/var/log/etl.log"},
    ],
    "channels": {
        "oncall": {"type": "slack", "channel": "C1", "token": "${env:SLACK_BOT_TOKEN}", "interactive": True},
        "finance": {"type": "webhook", "url": "https://hooks.example.com/${env:HOOK}"},
    },
    "routes": [
        {"match": {"labels": {"domain": "finance"}}, "notify": ["finance"], "approve": "oncall"},
        {"default": {"notify": ["oncall"]}},
    ],
    "actions": [{"id": "rerun-export", "runner": "etl-box", "risk": "low"}],
    "policies": [{"match": {"risk": "low"}, "decision": "auto"}],
}
ENV = {"AM_TOKEN": "am-secret-123", "SLACK_BOT_TOKEN": "xoxb-1", "HOOK": "abc"}


def test_a_four_line_spec_is_valid() -> None:
    loaded = parse_spec({"workspace": "team-a"}, env={})

    assert loaded.spec.workspace == "team-a"
    assert loaded.spec.environment == "production"
    assert loaded.spec.sources == []


def test_secret_references_resolve_and_are_collected_for_redaction() -> None:
    loaded = parse_spec(FULL, env=ENV)

    assert loaded.spec.source("prometheus").options["token"] == "am-secret-123"
    assert loaded.spec.channels["finance"].options["url"] == "https://hooks.example.com/abc"
    assert {"am-secret-123", "xoxb-1"} <= loaded.secret_values


def test_every_missing_secret_is_reported_at_once() -> None:
    with pytest.raises(SpecError) as error:
        parse_spec(FULL, env={})

    text = str(error.value)
    assert "AM_TOKEN" in text and "SLACK_BOT_TOKEN" in text and "HOOK" in text


def test_file_secrets_strip_the_trailing_newline(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("s3cret\n")

    assert secrets.resolve({"a": f"${{file:{secret}}}"}, env={}) == {"a": "s3cret"}


def test_unknown_secret_provider_is_rejected() -> None:
    with pytest.raises(secrets.SecretResolutionError, match="unknown secret provider 'vault'"):
        secrets.resolve("${vault:kv/x}", env={})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"routes": [{"notify": ["nope"]}]}, "unknown channel 'nope'"),
        ({"routes": [{"notify": ["finance"], "approve": "finance"}]}, "is not interactive"),
        ({"actions": [{"id": "x", "kind": "runner"}]}, "names no runner"),
        ({"sources": [{"name": "a", "type": "webhook"}, {"name": "a", "type": "webhook"}]}, "used more than once"),
        ({"policies": [{"match": {"actions": ["ghost"]}, "decision": "auto"}]}, "unknown action 'ghost'"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_invalid_references_are_explained(change: dict, message: str) -> None:
    with pytest.raises(SpecError, match=message):
        parse_spec({**FULL, **change}, env=ENV)


def _incident(severity: Severity = Severity.HIGH, labels: dict | None = None) -> Incident:
    return Incident(incident_id="i", workspace_id="w", fingerprint="f", title="t", severity=severity,
                    status=IncidentStatus.OPEN, labels=labels or {}, opened_at=utcnow(), updated_at=utcnow())


def test_route_matching_uses_severity_floor_labels_and_source() -> None:
    from sentinel.core.spec import RouteMatch

    match = RouteMatch(severity_at_least="high", labels={"domain": "finance"}, sources=["prometheus"])

    assert match.matches(severity=Severity.CRITICAL, source="prometheus", labels={"domain": "finance"})
    assert not match.matches(severity=Severity.MEDIUM, source="prometheus", labels={"domain": "finance"})
    assert not match.matches(severity=Severity.HIGH, source="other", labels={"domain": "finance"})
    assert not match.matches(severity=Severity.HIGH, source="prometheus", labels={})


def test_severity_parsing_understands_common_spellings() -> None:
    assert Severity.parse("P1") is Severity.CRITICAL
    assert Severity.parse("warning") is Severity.MEDIUM
    assert Severity.parse("Error") is Severity.HIGH
    assert Severity.parse("gibberish", Severity.LOW) is Severity.LOW


def test_a_directory_holds_one_workspace_per_file(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("workspace: team-a\n")
    (tmp_path / "b.yml").write_text("workspace: team-b\n")

    registry = WorkspaceRegistry.from_path(tmp_path, env={})

    assert registry.names() == ["team-a", "team-b"]
    assert registry.get("team-a").text == "workspace: team-a\n"


def test_two_files_cannot_claim_the_same_workspace(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("workspace: team-a\n")
    (tmp_path / "b.yaml").write_text("workspace: team-a\n")

    with pytest.raises(SpecError, match="also defined in a.yaml"):
        WorkspaceRegistry.from_path(tmp_path, env={})
