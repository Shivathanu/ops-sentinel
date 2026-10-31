import hashlib
import hmac
import json
from dataclasses import dataclass, field

import pytest

from sentinel.channels.base import ChannelConfigError, IncidentMessage
from sentinel.channels.router import ChannelFactoryRegistry, plan_routes
from sentinel.channels.slack import SlackChannel
from sentinel.channels.webhook import WebhookChannel
from sentinel.core.models import Incident, IncidentStatus, Severity, utcnow
from sentinel.core.spec import parse_spec
from sentinel.store.queue import Job
from sentinel.workers.handlers import Handlers, PermanentJobError

SPEC = {
    "workspace": "team-a",
    "channels": {
        "oncall": {"type": "slack", "channel": "C1", "token": "xoxb-1", "interactive": True},
        "finance": {"type": "webhook", "url": "https://hooks.example.com/f"},
        "pager": {"type": "webhook", "url": "https://hooks.example.com/p"},
    },
    "routes": [
        {"match": {"severity_at_least": "critical"}, "notify": ["pager"], "continue_matching": True},
        {"match": {"labels": {"domain": "finance"}}, "notify": ["finance"], "approve": "oncall"},
        {"default": {"notify": ["oncall"], "approve": "oncall"}},
    ],
}


def _incident(severity: Severity = Severity.HIGH, labels: dict | None = None,
              status: IncidentStatus = IncidentStatus.OPEN) -> Incident:
    return Incident(incident_id="INC1", workspace_id="team-a", fingerprint="f", title="Export failed", severity=severity,
                    status=status, labels=labels or {}, source="am", opened_at=utcnow(), updated_at=utcnow())


def test_first_match_wins_unless_a_route_continues() -> None:
    spec = parse_spec(SPEC, env={}).spec

    finance = plan_routes(spec, _incident(labels={"domain": "finance"}))
    assert (finance.notify, finance.approve) == (("finance",), "oncall")

    critical_finance = plan_routes(spec, _incident(Severity.CRITICAL, {"domain": "finance"}))
    assert critical_finance.notify == ("pager", "finance") and critical_finance.matched_routes == (0, 1)

    other = plan_routes(spec, _incident())
    assert other.channels == ("oncall",)


def test_without_routes_every_channel_is_notified_and_the_interactive_one_approves() -> None:
    spec = parse_spec({**SPEC, "routes": []}, env={}).spec

    plan = plan_routes(spec, _incident())

    assert set(plan.notify) == {"oncall", "finance", "pager"} and plan.approve == "oncall"


def test_channel_construction_reports_every_problem() -> None:
    spec = parse_spec({"workspace": "w", "channels": {"a": {"type": "slack"}, "b": {"type": "carrier-pigeon"},
                                                       "c": {"type": "webhook", "url": "ftp://x"}}}, env={}).spec

    with pytest.raises(ChannelConfigError) as error:
        ChannelFactoryRegistry(load_plugins=False).build(spec)

    text = str(error.value)
    assert "needs 'channel'" in text and "unknown type 'carrier-pigeon'" in text and "needs 'url'" in text


class _SlackClient:
    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.updated: list[dict] = []

    def chat_postMessage(self, **kwargs):  # noqa: N802 - Slack SDK name
        self.posted.append(kwargs)
        return {"channel": "C1", "ts": "111.222"}

    def chat_update(self, **kwargs):
        self.updated.append(kwargs)


def test_slack_sends_blocks_and_updates_in_place() -> None:
    client = _SlackClient()
    channel = SlackChannel("oncall", {"channel": "#oncall"}, client=client)

    ref = channel.send(IncidentMessage(_incident(Severity.CRITICAL), "Export failed"))
    channel.update(ref, IncidentMessage(_incident(), "Resolved"))

    assert ref == "C1:111.222"
    assert client.posted[0]["text"] == "[CRITICAL] Export failed"
    assert client.updated[0]["channel"] == "C1" and client.updated[0]["ts"] == "111.222"


class _Http:
    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes, dict]] = []

    def post(self, url: str, content: bytes, headers: dict):
        self.requests.append((url, content, headers))
        return type("R", (), {"raise_for_status": lambda self: None})()


def test_webhook_posts_signed_json() -> None:
    http = _Http()
    channel = WebhookChannel("hook", {"url": "https://x.example/h", "secret": "k3y"}, client=http)

    channel.send(IncidentMessage(_incident(), "Export failed"))

    url, body, headers = http.requests[0]
    assert json.loads(body)["incident"]["incident_id"] == "INC1"
    assert headers["X-Sentinel-Signature"] == "sha256=" + hmac.new(b"k3y", body, hashlib.sha256).hexdigest()


# Handlers -----------------------------------------------------------------------------------------------------------

@dataclass
class _Channel:
    name: str
    fail: bool = False
    sent: list = field(default_factory=list)
    updates: list = field(default_factory=list)
    interactive: bool = False
    kind: str = "fake"

    def send(self, message: IncidentMessage) -> str:
        if self.fail:
            raise RuntimeError("down")
        self.sent.append(message)
        return f"ref-{self.name}"

    def update(self, ref: str, message: IncidentMessage) -> None:
        self.updates.append((ref, message))


class _Store:
    def __init__(self, incident: Incident | None) -> None:
        self.incident = incident
        self.notifications: dict[str, str] = {}
        self.events: list[str] = []
        self.transitions: list = []

    def get(self, workspace: str, incident_id: str):
        return self.incident if self.incident and workspace == self.incident.workspace_id else None

    def add_event(self, workspace, incident_id, kind, actor, detail=None):
        self.events.append(kind)

    def notification_refs(self, workspace, incident_id):
        return dict(self.notifications)

    def summary(self, workspace, incident_id):
        return None

    def record_notification(self, workspace, incident_id, channel, purpose, ref):
        self.notifications[channel] = ref

    def transition(self, workspace, incident_id, *, to, allowed_from, actor, detail=None):
        self.transitions.append(to)
        self.incident = self.incident.model_copy(update={"status": to})
        return True


class _Platform:
    def __init__(self, store: _Store, channels: dict) -> None:
        self.store = store
        self._channels = channels
        self._loaded = parse_spec(SPEC, env={})

    def workspace(self, name):
        return type("E", (), {"loaded": self._loaded})() if name == "team-a" else None

    def channels(self, workspace):
        return self._channels


def _job(kind: str, **payload) -> Job:
    return Job("J", "default", kind, "team-a", {"incident_id": "INC1", **payload}, 1, 5)


def test_opened_incident_is_announced_once_per_channel_even_when_retried() -> None:
    store = _Store(_incident(Severity.CRITICAL))
    channels = {name: _Channel(name) for name in ("oncall", "finance", "pager")}
    channels["oncall"].fail = True
    handlers = Handlers(_Platform(store, channels))

    with pytest.raises(RuntimeError, match="oncall: down"):
        handlers.incident_opened(_job("incident.opened"))
    channels["oncall"].fail = False
    handlers.incident_opened(_job("incident.opened"))

    assert len(channels["pager"].sent) == 1  # not re-sent on the retry
    assert channels["oncall"].sent[0].purpose == "approve"
    assert set(store.notifications) == {"pager", "oncall"}


def test_recovery_resolves_an_untouched_incident_and_updates_messages() -> None:
    store = _Store(_incident())
    store.notifications = {"oncall": "ref-oncall"}
    oncall = _Channel("oncall")
    Handlers(_Platform(store, {"oncall": oncall})).incident_signal(_job("incident.signal", resolved=True))

    assert store.transitions == [IncidentStatus.RESOLVED]
    assert oncall.updates[0][1].headline == "Resolved: Export failed"


def test_recovery_does_not_close_an_incident_awaiting_approval() -> None:
    store = _Store(_incident(status=IncidentStatus.AWAITING_APPROVAL))
    Handlers(_Platform(store, {})).incident_signal(_job("incident.signal", resolved=True))

    assert store.transitions == []


def test_a_missing_incident_is_a_permanent_failure() -> None:
    with pytest.raises(PermanentJobError):
        Handlers(_Platform(_Store(None), {})).incident_opened(_job("incident.opened"))
