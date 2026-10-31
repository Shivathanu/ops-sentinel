"""Tools the agents may call, grouped in toolsets, with the guarantees the loop relies on.

- Every tool declares a capability. The loop executes only `read` tools. A `write` tool can never run from model
  output: it becomes a proposed action that goes through policy and approval (phase 3). A prompt-injected log line
  therefore cannot make the agent change anything.
- Arguments are validated against the tool's JSON schema (required keys, types of top-level fields) before the
  handler runs, and failures go back to the model as an error result it can correct.
- Output is bounded: each tool has a character limit, and longer results are cut with a marker, so one huge query
  result cannot flood the context window. The full output is kept as evidence.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from sentinel.llm.types import ToolSpec

JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,), "object": (dict,), "array": (list,),
}


@dataclass(frozen=True)
class ToolContext:
    workspace_id: str
    incident_id: str | None = None
    run_id: str | None = None
    secrets: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ToolOutput:
    content: str
    is_error: bool = False
    # Full, untruncated output for the evidence store when `content` is a summary of it.
    evidence: str | None = None


Handler = Callable[[dict[str, Any], ToolContext], "ToolOutput | str | dict | list"]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    capability: Literal["read", "write"] = "read"
    max_output_chars: int = 12_000
    toolset: str = ""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.input_schema)


def schema(properties: dict[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def validate_arguments(tool: Tool, arguments: dict[str, Any]) -> str | None:
    """Returns why the arguments are invalid, or None. Checks required keys, unknown keys and top-level types."""
    props: dict[str, Any] = tool.input_schema.get("properties", {})
    missing = [key for key in tool.input_schema.get("required", []) if key not in arguments]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"
    if tool.input_schema.get("additionalProperties") is False:
        unknown = [key for key in arguments if key not in props]
        if unknown:
            return f"unknown argument(s): {', '.join(unknown)}; expected: {', '.join(props) or 'none'}"
    for key, value in arguments.items():
        expected = props.get(key, {}).get("type")
        if expected in JSON_TYPES and not isinstance(value, JSON_TYPES[expected]):
            return f"argument '{key}' must be of type {expected}"
        if expected in ("integer", "number") and isinstance(value, bool):
            return f"argument '{key}' must be of type {expected}"
        allowed = props.get(key, {}).get("enum")
        if allowed and value not in allowed:
            return f"argument '{key}' must be one of: {', '.join(map(str, allowed))}"
    return None


def render(result: ToolOutput | str | dict | list, limit: int) -> ToolOutput:
    """Normalizes a handler's return value and enforces the output limit."""
    if not isinstance(result, ToolOutput):
        text = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False, indent=1)
        result = ToolOutput(text)
    if len(result.content) <= limit:
        return result
    kept = result.content[:limit]
    note = f"\n…[truncated: {len(result.content) - limit} more characters. Narrow the query to see them.]"
    return ToolOutput(kept + note, result.is_error, result.evidence or result.content)


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "[REDACTED]")
    return text


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, *tools: Tool) -> ToolRegistry:
        for tool in tools:
            if tool.name in self.tools:
                raise ValueError(f"tool '{tool.name}' is registered twice")
            self.tools[tool.name] = tool
        return self

    def subset(self, toolsets: Iterable[str]) -> ToolRegistry:
        wanted = set(toolsets)
        return ToolRegistry({name: t for name, t in self.tools.items() if t.toolset in wanted})

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def specs(self) -> list[ToolSpec]:
        # Sorted, so the tool list (and with it the prompt cache prefix) is byte-identical across runs.
        return [self.tools[name].spec for name in sorted(self.tools)]
