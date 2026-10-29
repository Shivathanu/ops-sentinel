import pytest

from ops_sentinel import security
from ops_sentinel.config import settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", frozenset()),
        ("  ", frozenset()),
        ("U1", frozenset({"U1"})),
        (" U1, U2 ,,U3 ", frozenset({"U1", "U2", "U3"})),
    ],
)
def test_approver_ids_are_parsed_from_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: frozenset[str]
) -> None:
    monkeypatch.setattr(settings, "slack_approver_ids", raw)

    assert settings.approver_ids() == expected


def test_startup_warnings_flag_every_unsafe_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "slack_approver_ids", "")
    monkeypatch.setattr(settings, "exasol_executor_user", "")

    warnings = " ".join(security.startup_warnings())

    assert "OPS_SENTINEL_API_KEY" in warnings
    assert "SLACK_APPROVER_IDS" in warnings
    assert "EXASOL_EXECUTOR_USER" in warnings


def test_a_fully_configured_server_has_no_startup_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "api_key", "k")
    monkeypatch.setattr(settings, "api_auth_disabled", False)
    monkeypatch.setattr(settings, "slack_approver_ids", "U1")
    monkeypatch.setattr(settings, "exasol_executor_user", "sentinel_executor")
    monkeypatch.setattr(settings, "exasol_validate_certificate", True)

    assert security.startup_warnings() == []


def test_startup_warns_about_a_missing_context_file_and_an_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "business_context_file", "/nonexistent/context.md")
    monkeypatch.setattr(settings, "llm_provider", "gpt-five")

    text = "\n".join(security.startup_warnings())

    assert "BUSINESS_CONTEXT_FILE" in text
    assert "LLM_PROVIDER" in text
