"""Provider-neutral types for the agent loop.

A `ChatSession` owns the provider's native message history. That matters: providers attach state to their own
message shapes (Claude's thinking blocks must be sent back unchanged; OpenAI tool calls must be echoed with their
ids), and translating history through a neutral format on every turn loses it. The agent loop therefore only
speaks in turns, tool calls and tool results, and each adapter keeps its own transcript.
"""
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Set when the provider returned arguments that are not a JSON object; the loop reports it to the model.
    parse_error: str | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


# end: the model finished its turn. tool_use: it wants tool results. max_tokens: cut off. refusal: declined.
STOP_REASONS = ("end", "tool_use", "max_tokens", "refusal")


@dataclass(frozen=True)
class AssistantTurn:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: Usage
    model: str
    detail: str = ""  # e.g. the refusal category


class ChatSession(Protocol):
    model: str

    def add_user(self, text: str) -> None: ...

    def complete(self) -> AssistantTurn:
        """Sends the transcript and appends the assistant's reply to it."""
        ...

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Answers every tool call of the last turn, in one message, as providers require."""
        ...


class ModelProvider(Protocol):
    def start(self, system: str, tools: list[ToolSpec]) -> ChatSession: ...


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_read_tokens * self.cache_read
            + usage.cache_write_tokens * self.cache_write
        ) / 1_000_000


@dataclass
class ModelBinding:
    """A model role resolved to a provider instance, with what it costs."""

    role: str
    provider: ModelProvider
    model: str
    price: Price = field(default_factory=lambda: Price(0.0, 0.0))
