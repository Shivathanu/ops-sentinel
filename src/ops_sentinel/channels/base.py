"""The channel contract and the message every channel renders."""
from dataclasses import dataclass, field
from typing import Any, Protocol

from ops_sentinel.core.models import Incident


class ChannelConfigError(ValueError):
    pass


@dataclass(frozen=True)
class IncidentMessage:
    incident: Incident
    headline: str
    body: str = ""
    # notify: first announcement; approve: carries the approval request; update: a change to an earlier message
    purpose: str = "notify"
    facts: dict[str, str] = field(default_factory=dict)
    link: str | None = None


class Channel(Protocol):
    name: str
    kind: str
    interactive: bool

    def send(self, message: IncidentMessage) -> str | None:
        """Delivers the message; returns a reference that `update` accepts (or None if updates are unsupported)."""
        ...

    def update(self, ref: str, message: IncidentMessage) -> None: ...


def incident_facts(incident: Incident) -> dict[str, Any]:
    facts = {"Severity": incident.severity.value.upper(), "Status": incident.status.value, "Signals": str(incident.signal_count)}
    if incident.service:
        facts["Service"] = incident.service
    return facts
