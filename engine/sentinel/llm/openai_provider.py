"""OpenAI Chat Completions with function calling; also serves Ollama and any OpenAI-compatible server
(vLLM, LM Studio, Azure OpenAI through its compatible endpoint), which is what makes a fully local setup possible."""
import json
from typing import Any

from openai import OpenAI

from sentinel.llm.types import AssistantTurn, ToolCall, ToolResult, ToolSpec, Usage

STOP_REASON = {"stop": "end", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal",
               "function_call": "tool_use"}


class OpenAICompatibleProvider:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_output_tokens: int = 4096,
        timeout_seconds: float = 120.0,
        client: Any = None,
    ):
        self.model = model
        self._max_tokens = max_output_tokens
        # Local servers ignore the key, but the client refuses to start without one.
        self._client = client or OpenAI(api_key=api_key or "not-needed", base_url=base_url, timeout=timeout_seconds)

    def start(self, system: str, tools: list[ToolSpec]) -> "OpenAISession":
        return OpenAISession(self, system, tools)


class OpenAISession:
    def __init__(self, provider: OpenAICompatibleProvider, system: str, tools: list[ToolSpec]):
        self.model = provider.model
        self._provider = provider
        self._tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
            for t in tools
        ]
        self._messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    def add_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def complete(self) -> AssistantTurn:
        params: dict[str, Any] = {"model": self.model, "messages": self._messages, "max_tokens": self._provider._max_tokens}
        if self._tools:
            params["tools"] = self._tools
        response = self._provider._client.chat.completions.create(**params)
        choice = response.choices[0]
        message = choice.message

        calls: list[ToolCall] = []
        echoed: list[dict[str, Any]] = []
        for call in message.tool_calls or []:
            raw = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw)
                error = None if isinstance(arguments, dict) else "arguments must be a JSON object"
            except ValueError as exc:
                arguments, error = {}, f"arguments are not valid JSON: {exc}"
            calls.append(ToolCall(call.id, call.function.name, arguments if isinstance(arguments, dict) else {}, error))
            echoed.append({"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": raw}})

        assistant: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
        if echoed:
            assistant["tool_calls"] = echoed
        self._messages.append(assistant)

        usage = response.usage
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        return AssistantTurn(
            text=message.content or "",
            tool_calls=calls,
            # Some local servers report "stop" even when they returned tool calls.
            stop_reason="tool_use" if calls else STOP_REASON.get(choice.finish_reason or "", "end"),
            usage=Usage(
                input_tokens=((usage.prompt_tokens or 0) - cached) if usage else 0,
                output_tokens=(usage.completion_tokens or 0) if usage else 0,
                cache_read_tokens=cached,
            ),
            model=getattr(response, "model", None) or self.model,
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for result in results:
            content = f"ERROR: {result.content}" if result.is_error else result.content
            self._messages.append({"role": "tool", "tool_call_id": result.call_id, "content": content})
