"""Decides which channels an incident reaches, and builds channel instances from the spec."""
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any

from sentinel.channels.base import Channel, ChannelConfigError
from sentinel.channels.slack import SlackChannel
from sentinel.channels.webhook import WebhookChannel
from sentinel.core.models import Incident
from sentinel.core.spec import WorkspaceSpec

logger = logging.getLogger("sentinel.router")

ChannelFactory = Callable[..., Channel]
BUILTIN_CHANNELS: dict[str, ChannelFactory] = {"slack": SlackChannel, "webhook": WebhookChannel}


@dataclass(frozen=True)
class RoutePlan:
    notify: tuple[str, ...] = ()
    approve: str | None = None
    matched_routes: tuple[int, ...] = field(default=())

    @property
    def channels(self) -> tuple[str, ...]:
        ordered = list(self.notify)
        if self.approve and self.approve not in ordered:
            ordered.append(self.approve)
        return tuple(ordered)


def plan_routes(spec: WorkspaceSpec, incident: Incident) -> RoutePlan:
    """First matching route wins unless it sets `continue_matching`. No routes at all: every channel is notified,
    so a brand-new workspace with one channel works without writing a route."""
    if not spec.routes:
        interactive = next((name for name, ch in spec.channels.items() if ch.interactive), None)
        return RoutePlan(tuple(spec.channels), interactive)

    notify: list[str] = []
    approve: str | None = None
    matched: list[int] = []
    for index, route in enumerate(spec.routes):
        if route.match is not None and not route.match.matches(
            severity=incident.severity, source=incident.source, labels=incident.labels
        ):
            continue
        matched.append(index)
        notify += [name for name in route.notify if name not in notify]
        approve = approve or route.approve
        if not route.continue_matching:
            break
    return RoutePlan(tuple(notify), approve, tuple(matched))


class ChannelFactoryRegistry:
    def __init__(self, factories: dict[str, ChannelFactory] | None = None, *, load_plugins: bool = True):
        self._factories = dict(BUILTIN_CHANNELS if factories is None else factories)
        if load_plugins:
            for entry in entry_points(group="sentinel.channels"):
                try:
                    self._factories[entry.name] = entry.load()
                except Exception:  # noqa: BLE001
                    logger.exception("could not load channel plugin %r", entry.name)

    def build(self, spec: WorkspaceSpec, **overrides: Any) -> dict[str, Channel]:
        """Instantiates every channel in the spec, reporting all configuration problems together."""
        channels: dict[str, Channel] = {}
        problems: list[str] = []
        for name, channel_spec in spec.channels.items():
            factory = self._factories.get(channel_spec.type)
            if factory is None:
                problems.append(f"channel '{name}': unknown type '{channel_spec.type}' (known: {', '.join(sorted(self._factories))})")
                continue
            try:
                channels[name] = factory(name, channel_spec.options, interactive=channel_spec.interactive, **overrides)
            except ChannelConfigError as exc:
                problems.append(str(exc))
        if problems:
            raise ChannelConfigError("; ".join(problems))
        return channels
