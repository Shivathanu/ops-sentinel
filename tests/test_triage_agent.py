import json
from datetime import datetime

import pytest

from ops_sentinel import triage_agent


def test_user_prompt_carries_failure_context_and_lineage() -> None:
    prompt = triage_agent._build_user_prompt({"LOG_ID": 7}, [{"TARGET_TABLE": "sales_clean"}])

    assert json.loads(prompt) == {
        "pipeline_failure": {"LOG_ID": 7},
        "downstream_lineage": [{"TARGET_TABLE": "sales_clean"}],
    }


def test_user_prompt_stringifies_values_json_cannot_encode() -> None:
    prompt = triage_agent._build_user_prompt({"EXECUTION_TS": datetime(2026, 10, 26, 9, 0)}, [])

    assert json.loads(prompt)["pipeline_failure"]["EXECUTION_TS"] == "2026-10-26 09:00:00"


@pytest.mark.parametrize(
    ("provider", "expected_backend"),
    [("openai", "_triage_openai"), ("anthropic", "_triage_anthropic")],
)
def test_triage_routes_to_the_configured_provider(
    monkeypatch: pytest.MonkeyPatch, provider: str, expected_backend: str
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(triage_agent.settings, "llm_provider", provider)
    for backend in ("_triage_openai", "_triage_anthropic"):
        monkeypatch.setattr(
            triage_agent, backend, lambda prompt, name=backend: calls.append(name) or {"risk_level": "LOW"}
        )

    result = triage_agent.triage({"LOG_ID": 1}, [])

    assert calls == [expected_backend]
    assert result == {"risk_level": "LOW"}


def test_an_unknown_provider_is_an_error_not_a_silent_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage_agent.settings, "llm_provider", "gpt-five")

    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        triage_agent.triage({"LOG_ID": 1}, [])


def test_ollama_routes_to_its_own_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage_agent.settings, "llm_provider", "ollama")
    monkeypatch.setattr(triage_agent, "_triage_ollama", lambda prompt: {"risk_level": "LOW"})

    assert triage_agent.triage({"LOG_ID": 1}, []) == {"risk_level": "LOW"}


def test_ollama_uses_the_local_endpoint_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    class FakeOpenAI:
        def __init__(self, api_key: str, base_url: str | None) -> None:
            seen.update(api_key=api_key, base_url=base_url)
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            seen["model"] = kwargs["model"]
            message = type("M", (), {"content": '{"risk_level": "LOW"}'})
            return type("R", (), {"choices": [type("C", (), {"message": message})]})

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    monkeypatch.setattr(triage_agent.settings, "ollama_base_url", "http://ollama:11434/v1")
    monkeypatch.setattr(triage_agent.settings, "ollama_model", "qwen2.5:3b")

    assert triage_agent._triage_ollama("{}") == {"risk_level": "LOW"}
    assert seen == {"api_key": "ollama", "base_url": "http://ollama:11434/v1", "model": "qwen2.5:3b"}


def test_business_context_is_added_to_the_system_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    context = tmp_path / "context.md"
    context.write_text("Finance close runs on the 1st; never touch the ledger table.")
    monkeypatch.setattr(triage_agent.settings, "business_context_file", str(context))

    prompt = triage_agent._system_prompt()

    assert prompt.startswith(triage_agent.SYSTEM_PROMPT)
    assert "<business_context>\nFinance close runs on the 1st" in prompt


def test_no_business_context_leaves_the_prompt_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(triage_agent.settings, "business_context_file", "")

    assert triage_agent._system_prompt() == triage_agent.SYSTEM_PROMPT


def test_an_unreadable_business_context_file_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(triage_agent.settings, "business_context_file", str(tmp_path / "missing.md"))

    with pytest.raises(FileNotFoundError):
        triage_agent._system_prompt()


def test_an_oversized_business_context_is_truncated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    context = tmp_path / "big.md"
    context.write_text("x" * (triage_agent.MAX_CONTEXT_CHARS + 500))
    monkeypatch.setattr(triage_agent.settings, "business_context_file", str(context))

    assert len(triage_agent.business_context()) == triage_agent.MAX_CONTEXT_CHARS
