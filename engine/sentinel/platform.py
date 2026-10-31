"""Wires the platform together from settings: workspace specs, the store, the queue, sources and channels.

The api and the worker both call `get_platform()`. It returns None when no spec is configured
(OPS_SENTINEL_SPEC_FILE unset), in which case the engine serves the v1 endpoints only.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sentinel.channels.base import Channel
from sentinel.channels.router import ChannelFactoryRegistry
from sentinel.config import settings
from sentinel.core.workspaces import WorkspaceEntry, WorkspaceRegistry
from sentinel.ingest.sources import SourceRegistry
from sentinel.store.exasol import ConnectionFactory, platform_connection, readonly_connection

if TYPE_CHECKING:
    from sentinel.agents.service import IncidentAnalyst
from sentinel.store.incidents import IncidentStore
from sentinel.store.queue import JobQueue

logger = logging.getLogger("sentinel.platform")


@dataclass
class Platform:
    workspaces: WorkspaceRegistry
    store: IncidentStore
    queue: JobQueue
    sources: SourceRegistry
    channel_factories: ChannelFactoryRegistry
    connect: ConnectionFactory | None = None
    readonly_connect: ConnectionFactory | None = None
    _channels: dict[str, dict[str, Channel]] = field(default_factory=dict)
    _analysts: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def workspace(self, name: str) -> WorkspaceEntry | None:
        return self.workspaces.get(name)

    def analyst(self, workspace: str) -> IncidentAnalyst | None:
        """The workspace's AI SRE, or None when its spec configures no models (analysis is then skipped)."""
        entry = self.workspaces.get(workspace)
        if entry is None or not entry.loaded.spec.models:
            return None
        with self._lock:
            if workspace not in self._analysts:
                from sentinel.agents.service import IncidentAnalyst
                from sentinel.agents.toolsets import build_registry
                from sentinel.llm.gateway import ModelGateway
                from sentinel.store.agent_runs import ExasolRunRecorder
                from sentinel.triage_agent import business_context

                spec = entry.loaded.spec
                self._analysts[workspace] = IncidentAnalyst(
                    spec, self.store, ModelGateway(spec.models),
                    build_registry(self.store, spec, self.readonly_connect),
                    ExasolRunRecorder(self.connect) if self.connect else _null_recorder(),
                    business_context=business_context(), secrets=entry.loaded.secret_values,
                )
            return self._analysts[workspace]

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
            try:
                from sentinel.agents.definitions import resolve_definitions

                resolve_definitions(spec.agents, set(spec.toolsets) | {"incident"})
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


def build_platform(
    spec_path: str,
    connect: ConnectionFactory = platform_connection,
    readonly_connect: ConnectionFactory | None = None,
) -> Platform:
    return Platform(
        workspaces=WorkspaceRegistry.from_path(spec_path),
        store=IncidentStore(connect),
        queue=JobQueue(connect),
        sources=SourceRegistry(),
        channel_factories=ChannelFactoryRegistry(),
        connect=connect,
        readonly_connect=readonly_connect,
    )


def _null_recorder():
    from sentinel.agents.runtime import NullRecorder

    return NullRecorder()


_lock = threading.Lock()
_platform: Platform | None = None


def get_platform() -> Platform | None:
    global _platform
    if not settings.spec_file:
        return None
    with _lock:
        if _platform is None:
            _platform = build_platform(settings.spec_file, readonly_connect=readonly_connection)
    return _platform
