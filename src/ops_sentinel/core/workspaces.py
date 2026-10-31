"""Loads workspace specs: one `sentinel.yaml`, or a directory with one spec per team.

    OPS_SENTINEL_SPEC_FILE=/app/sentinel.yaml        a single workspace
    OPS_SENTINEL_SPEC_FILE=/app/workspaces/          every *.yaml / *.yml inside, one workspace each

A directory is how several teams share one deployment: each team owns its file (and can keep it in its own repo,
mounted in), and nothing a team writes can name another team's channels or actions.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ops_sentinel.core.spec import LoadedSpec, SpecError, load_spec


@dataclass(frozen=True)
class WorkspaceEntry:
    loaded: LoadedSpec
    path: Path
    text: str  # as written, secret references unresolved: safe to store and display


class WorkspaceRegistry:
    def __init__(self, entries: Mapping[str, WorkspaceEntry]):
        self._entries = dict(entries)

    @classmethod
    def from_path(cls, path: str | Path, env: Mapping[str, str] | None = None) -> "WorkspaceRegistry":
        root = Path(path)
        files = sorted([*root.glob("*.yaml"), *root.glob("*.yml")]) if root.is_dir() else [root]
        if not files:
            raise SpecError([f"no *.yaml workspace specs in {root}"])
        entries: dict[str, WorkspaceEntry] = {}
        problems: list[str] = []
        for file in files:
            try:
                loaded = load_spec(file, env)
            except SpecError as exc:
                problems += [f"{file.name}: {p}" for p in exc.problems]
                continue
            name = loaded.spec.workspace
            if name in entries:
                problems.append(f"{file.name}: workspace '{name}' is also defined in {entries[name].path.name}")
                continue
            entries[name] = WorkspaceEntry(loaded, file, file.read_text(encoding="utf-8"))
        if problems:
            raise SpecError(problems)
        return cls(entries)

    def get(self, workspace: str) -> WorkspaceEntry | None:
        return self._entries.get(workspace)

    def names(self) -> list[str]:
        return sorted(self._entries)

    def entries(self) -> list[WorkspaceEntry]:
        return [self._entries[name] for name in self.names()]
