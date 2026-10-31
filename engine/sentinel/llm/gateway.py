"""Resolves the spec's model roles (`smart`, `fast`, ...) to providers, with prices for cost accounting."""
from collections.abc import Mapping

from sentinel.core.spec import ModelRole
from sentinel.llm.types import ModelBinding, ModelProvider, Price

# US dollars per million tokens (Anthropic first-party rates). Unknown and local models cost 0 for budgeting;
# token budgets still apply to them.
PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, cache_read=0.20, cache_write=5.00),
    "claude-sonnet-5-5": Price(2.00, 10.00, cache_read=0.20, cache_write=2.50),
    "claude-haiku-5-5": Price(0.10, 0.50, cache_read=0.01, cache_write=0.125),
    "claude-fable-5-1": Price(10.00, 50.00, cache_read=0.25, cache_write=12.50),
    "gpt-4o-mini": Price(0.15, 0.60),
}


class ModelConfigError(ValueError):
    pass


def build_provider(role: ModelRole) -> ModelProvider:
    if role.provider == "anthropic":
        from sentinel.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(role.model, api_key=role.api_key, base_url=role.base_url,
                                 max_output_tokens=role.max_output_tokens, timeout_seconds=role.timeout_seconds)
    from sentinel.llm.openai_provider import OpenAICompatibleProvider

    base_url = role.base_url
    if role.provider == "ollama" and not base_url:
        base_url = "http://localhost:11434/v1"
    if role.provider == "openai_compatible" and not base_url:
        raise ModelConfigError(f"model '{role.model}': provider openai_compatible needs base_url")
    return OpenAICompatibleProvider(role.model, api_key=role.api_key, base_url=base_url,
                                    max_output_tokens=role.max_output_tokens, timeout_seconds=role.timeout_seconds)


class ModelGateway:
    """Hands agents a binding per role. A missing role falls back along `smart` ↔ `fast`, so one model is enough."""

    def __init__(self, roles: Mapping[str, ModelRole], *, provider_factory=build_provider):
        self._roles = dict(roles)
        self._factory = provider_factory
        self._cache: dict[str, ModelBinding] = {}

    @property
    def configured(self) -> bool:
        return bool(self._roles)

    def resolve(self, role: str) -> ModelBinding:
        name = role if role in self._roles else next((r for r in ("smart", "fast") if r in self._roles), None)
        if name is None:
            name = next(iter(self._roles), None)
        if name is None:
            raise ModelConfigError("no models are configured in the workspace spec (models: ...)")
        if name not in self._cache:
            spec = self._roles[name]
            self._cache[name] = ModelBinding(name, self._factory(spec), spec.model, PRICES.get(spec.model, Price(0.0, 0.0)))
        return self._cache[name]
