import pytest
from fastapi.testclient import TestClient
from slack_sdk.signature import SignatureVerifier

from ops_sentinel import main

# Not used as a context manager on purpose: that would run the startup hook and launch the watcher loop.
client = TestClient(main.app)

SLACK_FORM = {"content": b"payload=%7B%7D", "headers": {"content-type": "application/x-www-form-urlencoded"}}


def test_health_reports_ok() -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_scores_are_served_from_the_scoring_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "compute_trust_score", lambda: 97.5)
    monkeypatch.setattr(main, "compute_efficiency_score", lambda: 88.0)

    assert client.get("/api/trust-score").json() == {"trust_score": 97.5}
    assert client.get("/api/efficiency-score").json() == {"efficiency_score": 88.0}


def test_slack_endpoint_is_disabled_until_a_signing_secret_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "verifier", None)

    assert client.post("/slack/interactions", **SLACK_FORM).status_code == 503


def test_slack_endpoint_rejects_an_invalid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "verifier", SignatureVerifier("test-secret"))
    headers = {**SLACK_FORM["headers"], "X-Slack-Request-Timestamp": "1", "X-Slack-Signature": "v0=bad"}

    response = client.post("/slack/interactions", content=SLACK_FORM["content"], headers=headers)

    assert response.status_code == 401


@pytest.mark.xfail(reason="known gap: /api/* has no authentication")
def test_incident_api_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "recent_incidents", lambda: [])

    assert client.get("/api/incidents").status_code in {401, 403}
