"""Job handlers. Each one is idempotent: a job can run more than once (lease expiry, retry after a partial failure).

    incident.opened   route the incident, start its analysis, announce it on every planned channel not yet notified
    incident.analyze  the AI SRE triages and investigates; the analysis is attached and the announcements updated
    incident.signal   a repeat or recovery of an open incident: update the announcements; close it on recovery

Remediation handlers are added by phase 3 under their own job kinds; this module only maps kinds to functions.
"""
import logging
from collections.abc import Callable

from sentinel.agents.service import render_analysis
from sentinel.channels.base import IncidentMessage
from sentinel.channels.router import plan_routes
from sentinel.core.models import Incident, IncidentStatus
from sentinel.platform import Platform
from sentinel.store.incidents import INCIDENT_OPENED, INCIDENT_SIGNAL
from sentinel.store.queue import Job

logger = logging.getLogger("sentinel.handlers")

INCIDENT_ANALYZE = "incident.analyze"

Handler = Callable[[Job], None]
# A source recovery closes an incident only before anyone has started acting on it.
AUTO_RESOLVABLE = (IncidentStatus.OPEN, IncidentStatus.TRIAGING, IncidentStatus.INVESTIGATING)


class PermanentJobError(Exception):
    """Retrying cannot help (the incident or workspace is gone); the worker buries the job immediately."""


class Handlers:
    def __init__(self, platform: Platform):
        self._platform = platform

    def mapping(self) -> dict[str, Handler]:
        return {INCIDENT_OPENED: self.incident_opened, INCIDENT_SIGNAL: self.incident_signal,
                INCIDENT_ANALYZE: self.incident_analyze}

    def _load(self, job: Job) -> Incident:
        workspace = job.workspace_id or ""
        incident = self._platform.store.get(workspace, str(job.payload.get("incident_id", "")))
        if incident is None:
            raise PermanentJobError(f"incident {job.payload.get('incident_id')} not found in workspace {workspace!r}")
        if self._platform.workspace(workspace) is None:
            raise PermanentJobError(f"workspace {workspace!r} is no longer configured")
        return incident

    def incident_opened(self, job: Job) -> None:
        incident = self._load(job)
        spec = self._platform.workspace(incident.workspace_id).loaded.spec  # type: ignore[union-attr]
        plan = plan_routes(spec, incident)
        store = self._platform.store
        if spec.models:
            # Analysis starts right away and does not wait for (or depend on) channel delivery.
            self._platform.queue.enqueue(INCIDENT_ANALYZE, {"incident_id": incident.incident_id},
                                         workspace_id=incident.workspace_id,
                                         idempotency_key=f"analyze:{incident.incident_id}", max_attempts=2)
        if not plan.channels:
            store.add_event(incident.workspace_id, incident.incident_id, "unrouted", "system:router",
                            {"reason": "no route matched and no channel is configured"})
            return
        store.add_event(incident.workspace_id, incident.incident_id, "routed", "system:router",
                        {"notify": list(plan.notify), "approve": plan.approve, "routes": list(plan.matched_routes)})

        channels = self._platform.channels(incident.workspace_id)
        already = store.notification_refs(incident.workspace_id, incident.incident_id)
        failures = []
        for name in plan.channels:
            if name in already:
                continue  # sent by an earlier attempt of this job
            purpose = "approve" if name == plan.approve else "notify"
            try:
                ref = channels[name].send(_message(incident, purpose, analyzing=bool(spec.models)))
            except Exception as exc:  # noqa: BLE001 - one broken channel must not block the others
                logger.warning("channel %s failed for incident %s: %s", name, incident.incident_id, exc)
                failures.append(f"{name}: {exc}")
                continue
            store.record_notification(incident.workspace_id, incident.incident_id, name, purpose, ref)
        if failures:
            # Raising retries the job; channels that succeeded are skipped next time.
            raise RuntimeError("; ".join(failures))

    def incident_signal(self, job: Job) -> None:
        incident = self._load(job)
        store = self._platform.store
        if job.payload.get("resolved") and incident.status in AUTO_RESOLVABLE:
            store.transition(incident.workspace_id, incident.incident_id, to=IncidentStatus.RESOLVED,
                             allowed_from=AUTO_RESOLVABLE, actor=f"source:{incident.source}",
                             detail={"reason": "the source reported recovery"})
            incident = store.get(incident.workspace_id, incident.incident_id) or incident
        self._update_messages(incident)

    def incident_analyze(self, job: Job) -> None:
        incident = self._load(job)
        analyst = self._platform.analyst(incident.workspace_id)
        if analyst is None:
            return  # the spec no longer configures models
        if analyst.analyze(incident) is None:
            return  # resolved or taken over meanwhile
        self._update_messages(self._platform.store.get(incident.workspace_id, incident.incident_id) or incident)

    def _update_messages(self, incident: Incident) -> None:
        store = self._platform.store
        analysis = render_analysis(store.summary(incident.workspace_id, incident.incident_id))
        entry = self._platform.workspace(incident.workspace_id)
        analyzing = bool(entry and entry.loaded.spec.models) and incident.status in AUTO_RESOLVABLE
        channels = self._platform.channels(incident.workspace_id)
        for name, ref in store.notification_refs(incident.workspace_id, incident.incident_id).items():
            channel = channels.get(name)
            if channel is None or ref is None:
                continue
            try:
                channel.update(ref, _message(incident, "update", analysis, analyzing))
            except Exception as exc:  # noqa: BLE001 - an update is cosmetic; the timeline is the record
                logger.warning("could not update %s for incident %s: %s", name, incident.incident_id, exc)


def _message(incident: Incident, purpose: str, analysis: str = "", analyzing: bool = False) -> IncidentMessage:
    if incident.status is IncidentStatus.RESOLVED:
        return IncidentMessage(incident, f"Resolved: {incident.title}", body=analysis, purpose=purpose)
    parts = [analysis or ("_The AI SRE is analyzing this incident…_" if analyzing else "")]
    if purpose == "approve":
        parts.append("_This channel approves remediation for this incident. Proposed actions will appear here._")
    return IncidentMessage(incident, incident.title, body="\n\n".join(p for p in parts if p), purpose=purpose)
