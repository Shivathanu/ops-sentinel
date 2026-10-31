"""Wires the platform together from settings: workspace specs, the store, the queue, sources and channels.

The api and the worker both call `get_platform()`. It returns None when no spec is configured
(OPS_SENTINEL_SPEC_FILE unset), in which case the engine serves the v1 endpoints only.
"""
import logging
import threading
from dataclasses import dataclass, field

from ops_sentinel.channels.base import Channel
from ops_sentinel.channels.router import ChannelFactoryRegistry
from ops_sentinel.config import settings
from ops_sentinel.core.workspaces import WorkspaceEntry, WorkspaceRegistry
from ops_sentinel.ingest.sources import SourceRegistry
from ops_sentinel.store.exasol import ConnectionFactory, platform_connection
from ops_sentinel.store.incidents import IncidentStore
from ops_sentinel.store.queue import JobQueue

logger = logging.getLogger("ops_sentinel.platform")


@dataclass
class Platform:
    workspaces: WorkspaceRegistry
    store: IncidentStore
    queue: JobQueue
    sources: SourceRegistry
    channel_factories: ChannelFactoryRegistry
    _channels: dict[str, dict[str, Channel]] = field(default_factory=dict)

    def workspace(self, name: str) -> WorkspaceEntry | None:
        return self.workspaces.get(name)

    def channels(self, workspace: str) -> dict[str, Channel]:
        if workspace not in self._channels:
            entry = self.workspaces.get(workspace)
            self._channels[workspace] = {} if entry is None else self.channel_factories.build(entry.loaded.spec)
        return self._channels[workspace]

    def validate(self) -> list[str]:
        """Configuration problems that only show up when plugins read their options. Empty means ready."""
        problems = []
        for entry in self.workspaces.entries():
            spec = entry.loaded.spec
            for source in spec.sources:
                if source.runner is None and self.sources.get(source.type) is None:
                    problems.append(f"{spec.workspace}: source '{source.name}' has unknown type '{source.type}' "
                                    f"(known: {', '.join(self.sources.kinds())})")
            try:
                self.channels(spec.workspace)
            except ValueError as exc:
                problems.append(f"{spec.workspace}: {exc}")
        return problems

    def sync_workspaces(self, applied_by: str = "system:startup") -> None:
        """Registers each workspace and records its spec when it changed, so config history lives in Exasol."""
        for entry in self.workspaces.entries():
            name = entry.loaded.spec.workspace
            self.store.ensure_workspace(name)
            if self.store.record_config(name, entry.text, applied_by):
                logger.info("workspace '%s': recorded a new spec version from %s", name, entry.path)


def build_platform(spec_path: str, connect: ConnectionFactory = platform_connection) -> Platform:
    return Platform(
        workspaces=WorkspaceRegistry.from_path(spec_path),
        store=IncidentStore(connect),
        queue=JobQueue(connect),
        sources=SourceRegistry(),
        channel_factories=ChannelFactoryRegistry(),
    )


_lock = threading.Lock()
_platform: Platform | None = None


def get_platform() -> Platform | None:
    global _platform
    if not settings.spec_file:
        return None
    with _lock:
        if _platform is None:
            _platform = build_platform(settings.spec_file)
    return _platform
