"""The workspace spec (`sentinel.yaml`): everything a team configures, in one validated document.

Only the workspace name is required; every other section has a working default, so a first spec can be four lines.
Plugin-specific fields (a source's `path`, a channel's `webhook`) are kept as extra fields and validated by the
plugin that owns them, so adding a plugin never requires changing this module.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ops_sentinel.core import secrets
from ops_sentinel.core.models import Severity

NAME = r"^[A-Za-z0-9_.-]{1,100}$"


class SpecError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("invalid workspace spec:\n  - " + "\n  - ".join(problems))
        self.problems = problems


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Open(BaseModel):
    """Core fields are typed; anything else is plugin configuration, available via `options`."""

    model_config = ConfigDict(extra="allow", frozen=True)

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class ModelRole(_Strict):
    provider: Literal["openai", "anthropic", "ollama", "openai_compatible"]
    model: str
    base_url: str | None = None
    api_key: str | None = None
    max_output_tokens: int = Field(default=4096, ge=1, le=200_000)
    timeout_seconds: float = Field(default=60, gt=0, le=600)


class SourceSpec(_Open):
    name: str = Field(pattern=NAME)
    type: str = Field(min_length=1)
    # Where the source runs: omitted means in the engine (push sources such as webhooks); a runner name means on
    # that edge runner (log files, local commands).
    runner: str | None = Field(default=None, pattern=NAME)
    severity: Severity | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class KnowledgeSpec(_Open):
    name: str = Field(pattern=NAME)
    type: str = Field(min_length=1)
    pinned: bool = False


class ChannelSpec(_Open):
    type: str = Field(min_length=1)
    interactive: bool = False


class RouteMatch(_Strict):
    severity_at_least: Severity | None = None
    sources: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)

    def matches(self, *, severity: Severity, source: str | None, labels: Mapping[str, str]) -> bool:
        if self.severity_at_least is not None and severity.rank < self.severity_at_least.rank:
            return False
        if self.sources and source not in self.sources:
            return False
        return all(labels.get(key) == value for key, value in self.labels.items())


class RouteSpec(_Strict):
    match: RouteMatch | None = None  # no match: catch-all
    notify: list[str] = Field(default_factory=list)
    approve: str | None = None
    # Keep evaluating later routes after this one matches, so an incident can reach several audiences.
    continue_matching: bool = False

    @model_validator(mode="before")
    @classmethod
    def _default_shorthand(cls, data: Any) -> Any:
        # `- default: {notify: [...]}` reads naturally in YAML; it is a route without a match.
        if isinstance(data, dict) and set(data) == {"default"} and isinstance(data["default"], dict):
            return data["default"]
        return data


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionSpec(_Open):
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    description: str = ""
    # runner: the command lives in that runner's local config and never reaches the engine.
    # sql: engine-executed through the guardrail and the executor user. http: engine-executed request.
    kind: Literal["runner", "sql", "http"] = "runner"
    runner: str | None = Field(default=None, pattern=NAME)
    risk: Risk = Risk.MEDIUM
    params: dict[str, Any] = Field(default_factory=dict, description="JSON Schema of the parameters")
    verify: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _runner_needs_a_runner(self) -> ActionSpec:
        if self.kind == "runner" and not self.runner:
            raise ValueError(f"action '{self.id}' has kind 'runner' but names no runner")
        return self


class PolicyMatch(_Strict):
    risk: list[Risk] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)

    @field_validator("risk", "environments", "actions", mode="before")
    @classmethod
    def _scalar_to_list(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class PolicySpec(_Strict):
    match: PolicyMatch = Field(default_factory=PolicyMatch)
    decision: Literal["auto", "approve", "deny"]
    approvers: list[str] = Field(default_factory=list)
    quorum: int = Field(default=1, ge=1, le=5)


class Budget(_Strict):
    max_steps: int = Field(default=12, ge=1, le=200)
    max_tokens: int = Field(default=200_000, ge=1_000)
    max_seconds: int = Field(default=300, ge=5, le=3600)
    max_cost_usd: float = Field(default=1.0, gt=0)


class AgentSpec(_Open):
    name: str = Field(pattern=NAME)
    extends: str | None = None
    instructions: str | None = None
    model: str | None = None  # a model role name
    toolsets: list[str] | None = None
    subagents: list[str] | None = None
    budget: Budget | None = None


class RoutineSpec(_Open):
    name: str = Field(pattern=NAME)
    trigger: dict[str, Any]
    steps: list[dict[str, Any]] = Field(min_length=1)


class WorkspaceSpec(_Strict):
    api_version: Literal["sentinel/v1"] = Field(default="sentinel/v1", alias="apiVersion")
    workspace: str = Field(pattern=NAME)
    environment: str = Field(default="production", pattern=NAME)
    models: dict[str, ModelRole] = Field(default_factory=dict)
    sources: list[SourceSpec] = Field(default_factory=list)
    toolsets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    knowledge: list[KnowledgeSpec] = Field(default_factory=list)
    channels: dict[str, ChannelSpec] = Field(default_factory=dict)
    routes: list[RouteSpec] = Field(default_factory=list)
    actions: list[ActionSpec] = Field(default_factory=list)
    policies: list[PolicySpec] = Field(default_factory=list)
    agents: list[AgentSpec] = Field(default_factory=list)
    routines: list[RoutineSpec] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _references(self) -> WorkspaceSpec:
        problems: list[str] = []
        for label, names in (
            ("source", [s.name for s in self.sources]),
            ("action", [a.id for a in self.actions]),
            ("agent", [a.name for a in self.agents]),
            ("routine", [r.name for r in self.routines]),
            ("knowledge source", [k.name for k in self.knowledge]),
        ):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            problems += [f"{label} name '{n}' is used more than once" for n in duplicates]

        for index, route in enumerate(self.routes):
            for name in route.notify:
                if name not in self.channels:
                    problems.append(f"routes[{index}].notify names unknown channel '{name}'")
            if route.approve is not None:
                channel = self.channels.get(route.approve)
                if channel is None:
                    problems.append(f"routes[{index}].approve names unknown channel '{route.approve}'")
                elif not channel.interactive:
                    problems.append(f"routes[{index}].approve channel '{route.approve}' is not interactive")

        known_roles = set(self.models)
        for agent in self.agents:
            if agent.model is not None and agent.model not in known_roles:
                problems.append(f"agent '{agent.name}' uses unknown model role '{agent.model}'")
        action_ids = {a.id for a in self.actions}
        for index, policy in enumerate(self.policies):
            for action_id in policy.match.actions:
                if action_id not in action_ids:
                    problems.append(f"policies[{index}] names unknown action '{action_id}'")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def source(self, name: str) -> SourceSpec | None:
        return next((s for s in self.sources if s.name == name), None)

    def action(self, action_id: str) -> ActionSpec | None:
        return next((a for a in self.actions if a.id == action_id), None)


@dataclass(frozen=True)
class LoadedSpec:
    spec: WorkspaceSpec
    # Resolved secret values, for redaction in logs, audit rows and prompts.
    secret_values: frozenset[str] = field(default_factory=frozenset)


def parse_spec(raw: Any, env: Mapping[str, str] | None = None) -> LoadedSpec:
    if not isinstance(raw, dict):
        raise SpecError(["the spec must be a YAML mapping"])
    try:
        resolved = secrets.resolve(raw, env)
    except secrets.SecretResolutionError as exc:
        raise SpecError(exc.problems) from exc
    try:
        spec = WorkspaceSpec.model_validate(resolved)
    except ValidationError as exc:
        raise SpecError([_describe(error) for error in exc.errors()]) from exc
    return LoadedSpec(spec, frozenset(secrets.secret_values(raw, resolved)))


def load_spec(path: str | Path, env: Mapping[str, str] | None = None) -> LoadedSpec:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SpecError([f"cannot read {path}: {exc}"]) from exc
    return parse_spec(raw, env)


def _describe(error: Mapping[str, Any]) -> str:
    where = ".".join(str(part) for part in error.get("loc", ())) or "spec"
    message = str(error.get("msg", "invalid"))
    return f"{where}: {message.removeprefix('Value error, ')}"
