import json
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient
from slack_sdk.signature import SignatureVerifier

from ops_sentinel import main

# Not used as a context manager on purpose: that would run the lifespan and launch the watcher loop.
client = TestClient(main.app)

API_KEY = "test-api-key"
SIGNING_SECRET = "test-signing-secret"
APPROVER = "UAPPROVER"


@pytest.fixture(autouse=True)
def configured_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.settings, "api_key", API_KEY)
    monkeypatch.setattr(main.settings, "api_auth_disabled", False)
    monkeypatch.setattr(main.settings, "slack_approver_ids", APPROVER)
    monkeypatch.setattr(main, "verifier", SignatureVerifier(SIGNING_SECRET))
    monkeypatch.setattr(main, "compute_trust_score", lambda: 97.5)
    monkeypatch.setattr(main, "compute_efficiency_score", lambda: 88.0)
    monkeypatch.setattr(main, "recent_incidents", lambda: [])


def test_health_is_open() -> None:
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/api/trust-score", "/api/efficiency-score", "/api/incidents"])
def test_api_rejects_requests_without_a_key(path: str) -> None:
    assert client.get(path).status_code == 401


def test_api_rejects_a_wrong_key() -> None:
    assert client.get("/api/trust-score", headers={"X-API-Key": "wrong"}).status_code == 401


def test_api_serves_data_with_the_right_key() -> None:
    headers = {"X-API-Key": API_KEY}

    assert client.get("/api/trust-score", headers=headers).json() == {"trust_score": 97.5}
    assert client.get("/api/efficiency-score", headers=headers).json() == {"efficiency_score": 88.0}
    assert client.get("/api/incidents", headers=headers).json() == {"incidents": []}


def test_api_fails_closed_when_no_key_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.settings, "api_key", "")

    assert client.get("/api/trust-score", headers={"X-API-Key": ""}).status_code == 503


def test_api_auth_can_be_disabled_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.settings, "api_key", "")
    monkeypatch.setattr(main.settings, "api_auth_disabled", True)

    assert client.get("/api/trust-score").status_code == 200


def _slack_post(
    user_id: str = APPROVER, action_id: str = "approve_incident", payload: dict | None = None
) -> "object":
    payload = payload or {
        "user": {"id": user_id, "username": "alice"},
        "actions": [{"action_id": action_id, "value": "7"}],
        "message": {"ts": "1.2"},
    }
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    timestamp = str(int(time.time()))
    signature = SignatureVerifier(SIGNING_SECRET).generate_signature(timestamp=timestamp, body=body)
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
    }
    return client.post("/slack/interactions", content=body, headers=headers)


@pytest.fixture
def remediation_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(
        main, "execute_remediation", lambda incident_id, approved_by: calls.append(("approve", incident_id, approved_by)) or (True, "done")
    )
    monkeypatch.setattr(main, "reject_incident", lambda incident_id: calls.append(("reject", incident_id)))
    monkeypatch.setattr(main, "update_incident_card", lambda ts, text: calls.append(("card", ts, text)))
    return calls


def test_slack_endpoint_is_disabled_until_a_signing_secret_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "verifier", None)

    assert client.post("/slack/interactions", content=b"payload=%7B%7D").status_code == 503


def test_slack_endpoint_rejects_an_invalid_signature() -> None:
    headers = {"X-Slack-Request-Timestamp": "1", "X-Slack-Signature": "v0=bad"}

    assert client.post("/slack/interactions", content=b"payload=%7B%7D", headers=headers).status_code == 401


def test_authorized_approver_triggers_remediation_recorded_under_their_identity(
    remediation_calls: list[tuple],
) -> None:
    response = _slack_post()

    assert response.status_code == 200
    assert remediation_calls[0] == ("approve", 7, "alice (UAPPROVER)")
    assert remediation_calls[1][0] == "card"


def test_authorized_approver_can_reject(remediation_calls: list[tuple]) -> None:
    response = _slack_post(action_id="reject_incident")

    assert response.status_code == 200
    assert remediation_calls[0] == ("reject", 7)


def test_a_valid_slack_user_who_is_not_an_approver_is_refused(remediation_calls: list[tuple]) -> None:
    response = _slack_post(user_id="UINTRUDER")

    assert response.status_code == 403
    assert remediation_calls == []


def test_nobody_can_approve_when_no_approvers_are_configured(
    monkeypatch: pytest.MonkeyPatch, remediation_calls: list[tuple]
) -> None:
    monkeypatch.setattr(main.settings, "slack_approver_ids", "")

    assert _slack_post().status_code == 403
    assert remediation_calls == []


def test_unknown_slack_action_is_a_bad_request(remediation_calls: list[tuple]) -> None:
    assert _slack_post(action_id="delete_everything").status_code == 400
    assert remediation_calls == []


def test_malformed_slack_payload_is_a_bad_request(remediation_calls: list[tuple]) -> None:
    assert _slack_post(payload={"user": {"id": APPROVER}}).status_code == 400
    assert remediation_calls == []
