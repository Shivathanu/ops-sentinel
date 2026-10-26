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
