"""Push source plugins and their registry.

A source plugin has one job: turn a request body into Signals. Built-ins cover Alertmanager, Grafana, any JSON or
text webhook, and the edge runner (`sentinel watch`). Third-party plugins register under the `sentinel.sources`
entry point group and are picked up without code changes here:

    [project.entry-points."sentinel.sources"]
    datadog = "my_package.sources:DatadogSource"
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from importlib.metadata import entry_points
from typing import Any, Protocol

from sentinel.core.models import Severity, Signal, utcnow
from sentinel.core.spec import SourceSpec
from sentinel.udf_lib.fingerprint import fingerprint

logger = logging.getLogger("sentinel.sources")

MAX_SIGNALS_PER_REQUEST = 100
SUMMARY_FIELDS = ("summary", "title", "message", "error", "text", "description")
KEY_FIELDS = ("fingerprint", "key", "alert_id", "id", "dedup_key")
SEVERITY_FIELDS = ("severity", "level", "priority")
RESOLVED_WORDS = frozenset({"resolved", "ok", "recovered", "closed"})


class SourceError(ValueError):
    """The body could not be understood; reported to the sender as 400."""


class PushSource(Protocol):
    kind: str

    def parse(self, spec: SourceSpec, body: bytes, headers: Mapping[str, str]) -> list[Signal]: ...


def parse_time(value: Any) -> datetime:
    """ISO 8601 with 'Z' and up to nanosecond precision (Go's format) → naive UTC. Falls back to now."""
    if not isinstance(value, str) or not value or value.startswith("0001-01-01"):
        return utcnow()
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, _, rest = text.partition(".")
        width = len(rest) - len(rest.lstrip("0123456789"))
        # Python 3.10 accepts exactly 3 or 6 fractional digits.
        text = f"{head}.{rest[:min(width, 6)].ljust(6, '0')}{rest[width:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return utcnow()
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceError(f"body is not valid JSON: {exc}") from exc


def _str_labels(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k)[:100]: str(v)[:500] for k, v in raw.items() if v is not None}


def make_signal(
    spec: SourceSpec,
    *,
    summary: str,
    key_parts: tuple[Any, ...],
    severity: Any = None,
    labels: Mapping[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    resolved: bool = False,
    observed_at: datetime | None = None,
) -> Signal:
    """Applies what every source shares: spec labels win over payload labels, the spec severity is the default."""
    merged = {**dict(labels or {}), **spec.labels}
    default = spec.severity or Severity.MEDIUM
    return Signal(
        source=spec.name,
        fingerprint=fingerprint(spec.name, *key_parts),
        summary=(summary or f"{spec.name} alert").strip()[:2000],
        severity=Severity.parse(severity, default) if severity else default,
        labels=merged,
        payload=payload or {},
        resolved=resolved,
        observed_at=observed_at or utcnow(),
    )


class AlertmanagerSource:
    """Prometheus Alertmanager webhook (also Grafana unified alerting, which sends the same shape)."""

    kind = "alertmanager"

    def parse(self, spec: SourceSpec, body: bytes, headers: Mapping[str, str]) -> list[Signal]:
        data = _json(body)
        alerts = data.get("alerts") if isinstance(data, dict) else None
        if not isinstance(alerts, list):
            raise SourceError("expected an Alertmanager payload with an 'alerts' list")
        signals = []
        for alert in alerts[:MAX_SIGNALS_PER_REQUEST]:
            if not isinstance(alert, dict):
                continue
            labels = _str_labels(alert.get("labels"))
            annotations = _str_labels(alert.get("annotations"))
            name = labels.get("alertname", "alert")
            summary = annotations.get("summary") or annotations.get("description") or alert.get("title") or name
            # Alertmanager's own fingerprint is a hash of the label set; fall back to the labels themselves.
            key = alert.get("fingerprint") or json.dumps(labels, sort_keys=True)
            signals.append(make_signal(
                spec,
                summary=f"{name}: {summary}" if summary != name else name,
                key_parts=(key,),
                severity=labels.get("severity"),
                labels=labels,
                payload={"annotations": annotations, "generatorURL": alert.get("generatorURL"),
                         "startsAt": alert.get("startsAt"), "endsAt": alert.get("endsAt")},
                resolved=str(alert.get("status", "")).lower() == "resolved",
                observed_at=parse_time(alert.get("startsAt")),
            ))
        return signals


class GrafanaSource(AlertmanagerSource):
    kind = "grafana"


class WebhookSource:
    """Any tool that can POST: a JSON object, a JSON list of objects, or plain text.

    Field names are matched loosely (summary/title/message/error/...), and `options.fields` maps custom names:
        fields: {summary: alert.name, key: alert.uid, severity: alert.priority}
    """

    kind = "webhook"

    def parse(self, spec: SourceSpec, body: bytes, headers: Mapping[str, str]) -> list[Signal]:
        content_type = headers.get("content-type", "")
        text = body.decode("utf-8-sig", errors="replace").strip()
        if "json" in content_type or text[:1] in ("{", "["):
            data = _json(body)
            items = data if isinstance(data, list) else [data]
            mapping = spec.options.get("fields") or {}
            return [self._from_object(spec, item, mapping) for item in items[:MAX_SIGNALS_PER_REQUEST]
                    if isinstance(item, dict)]
        if not text:
            raise SourceError("empty body")
        return [make_signal(spec, summary=text[:2000], key_parts=(text,), payload={"text": text[:20000]})]

    @staticmethod
    def _from_object(spec: SourceSpec, item: dict[str, Any], mapping: Mapping[str, str]) -> Signal:
        def pick(role: str, candidates: tuple[str, ...]) -> Any:
            if role in mapping:
                return _dig(item, mapping[role])
            return next((item[c] for c in candidates if item.get(c) not in (None, "")), None)

        summary = pick("summary", SUMMARY_FIELDS)
        summary_text = str(summary) if summary is not None else json.dumps(item)[:500]
        key = pick("key", KEY_FIELDS)
        status = str(pick("status", ("status", "state")) or "").lower()
        labels = _str_labels(item.get("labels") or item.get("tags"))
        return make_signal(
            spec,
            summary=summary_text,
            key_parts=(key,) if key is not None else (summary_text,),
            severity=pick("severity", SEVERITY_FIELDS),
            labels=labels,
            payload=item,
            resolved=status in RESOLVED_WORDS,
        )


class RunnerSource:
    """Failures reported by the edge runner (`sentinel watch`), in the CLI's FailureEvent shape."""

    kind = "sentinel_runner"

    def parse(self, spec: SourceSpec, body: bytes, headers: Mapping[str, str]) -> list[Signal]:
        data = _json(body)
        if not isinstance(data, dict) or not data.get("job"):
            raise SourceError("expected a runner event with 'source', 'job' and 'error'")
        job, error = str(data["job"])[:200], str(data.get("error", ""))[-4000:]
        runner_source = str(data.get("source") or spec.name)[:100]
        last_line = next((line for line in reversed(error.splitlines()) if line.strip()), "")
        labels = {"job": job, "runner_source": runner_source}
        if data.get("fix_id"):
            labels["fix_id"] = str(data["fix_id"])
        return [make_signal(
            spec,
            summary=f"{runner_source}: {job} failed" + (f" — {last_line[:300]}" if last_line else ""),
            key_parts=(runner_source, job),
            severity=data.get("severity") or "high",
            labels=labels,
            payload={"error": error, "exit_code": data.get("exit_code"), "fix_id": data.get("fix_id")},
            resolved=bool(data.get("resolved")),
        )]


def _dig(item: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(item, dict):
            return None
        item = item.get(part)
    return item


BUILTIN: dict[str, PushSource] = {s.kind: s for s in (AlertmanagerSource(), GrafanaSource(), WebhookSource(), RunnerSource())}


class SourceRegistry:
    def __init__(self, sources: Mapping[str, PushSource] | None = None, *, load_plugins: bool = True):
        self._sources: dict[str, PushSource] = dict(BUILTIN if sources is None else sources)
        if load_plugins:
            self._load_entry_points()

    def _load_entry_points(self) -> None:
        for entry in entry_points(group="sentinel.sources"):
            try:
                plugin = entry.load()
                instance = plugin() if isinstance(plugin, type) else plugin
                self._sources[getattr(instance, "kind", entry.name)] = instance
            except Exception:  # noqa: BLE001 - a broken plugin must not take the engine down
                logger.exception("could not load source plugin %r", entry.name)

    def get(self, kind: str) -> PushSource | None:
        return self._sources.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._sources)
