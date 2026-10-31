"""Claude through the official Anthropic SDK (Messages API, manual tool-use loop).

- The system prompt and tool definitions are stable for a whole agent run, so both carry a cache breakpoint; every
  step after the first reads them from the prompt cache.
- The assistant's `response.content` is appended unchanged, which keeps thinking blocks valid across tool turns.
- All tool results of a turn go back in one user message; a failed tool is reported with `is_error`, never dropped.
- Current Claude models reject forced `tool_choice`, so the loop steers with instructions and `tool_choice: auto`.
- On models that support it, refused requests are retried by the API on a fallback model (`fallbacks: "default"`).
"""
from typing import Any

import anthropic

from sentinel.llm.types import AssistantTurn, ToolCall, ToolResult, ToolSpec, Usage

# Server-side refusal fallback: supported on these models with the scalar "default" form on the Claude API.
FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"

STOP_REASON = {"end_turn": "end", "tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal",
               "stop_sequence": "end", "pause_turn": "tool_use"}


class AnthropicProvider:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_output_tokens: int = 16000,
        timeout_seconds: float = 120.0,
        effort: str | None = None,
        client: Any = None,
    ):
        self.model = model
        self._max_tokens = max_output_tokens
        self._effort = effort
        self._client = client or anthropic.Anthropic(api_key=api_key or None, base_url=base_url, timeout=timeout_seconds)

    def start(self, system: str, tools: list[ToolSpec]) -> "AnthropicSession":
        return AnthropicSession(self, system, tools)


class AnthropicSession:
    def __init__(self, provider: AnthropicProvider, system: str, tools: list[ToolSpec]):
        self.model = provider.model
        self._provider = provider
        self._system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self._tools: list[dict[str, Any]] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
        ]
        if self._tools:
            self._tools[-1] = {**self._tools[-1], "cache_control": {"type": "ephemeral"}}
        self._messages: list[dict[str, Any]] = []

    def add_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def complete(self) -> AssistantTurn:
        provider = self._provider
        params: dict[str, Any] = {
            "model": provider.model,
            "max_tokens": provider._max_tokens,
            "system": self._system,
            "messages": self._messages,
        }
        if self._tools:
            params["tools"] = self._tools
        extra: dict[str, Any] = {}
        if provider._effort:
            extra["output_config"] = {"effort": provider._effort}
        if provider.model in FALLBACK_MODELS:
            extra["fallbacks"] = "default"
            response = provider._client.beta.messages.create(**params, betas=[FALLBACK_BETA], extra_body=extra)
        else:
            response = provider._client.messages.create(**params, **({"extra_body": extra} if extra else {}))

        # Appended exactly as returned: thinking and fallback blocks must round-trip unchanged.
        self._messages.append({"role": "assistant", "content": response.content})
        text = "".join(block.text for block in response.content if block.type == "text")
        calls = [
            ToolCall(block.id, block.name, block.input if isinstance(block.input, dict) else {},
                     None if isinstance(block.input, dict) else "tool input was not a JSON object")
            for block in response.content
            if block.type == "tool_use"
        ]
        usage = response.usage
        detail = ""
        if response.stop_reason == "refusal" and getattr(response, "stop_details", None):
            detail = f"refused ({getattr(response.stop_details, 'category', None) or 'unspecified'})"
        return AssistantTurn(
            text=text,
            tool_calls=calls,
            stop_reason=STOP_REASON.get(response.stop_reason or "", "end"),
            usage=Usage(
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            ),
            model=getattr(response, "model", provider.model),
            detail=detail,
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self._messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error}
                for r in results
            ],
        })
