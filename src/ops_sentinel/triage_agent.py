"""
Module B core: turns raw error/lineage context into a plain-language business
impact narrative and a suggested fix, via whichever LLM provider is configured.
"""
import json
import logging
from pathlib import Path

from ops_sentinel.config import settings

logger = logging.getLogger("ops_sentinel.triage_agent")

PROVIDERS = ("openai", "anthropic", "ollama")
MAX_CONTEXT_CHARS = 8000

SYSTEM_PROMPT = """You are Ops Sentinel's triage agent for a data platform.
You will be given a pipeline failure's error stack and its downstream data
lineage (which tables and dashboards depend on it). Respond with STRICT JSON
only, matching this schema, no prose outside the JSON:

{
  "business_impact": "<1-3 plain-language sentences a non-engineer exec would understand>",
  "root_cause": "<1-2 sentence technical root cause>",
  "suggested_fix_sql": "<a single SQL statement that would remediate or re-run the pipeline step, or null if no safe automated fix exists>",
  "risk_level": "LOW" | "MEDIUM" | "HIGH"
}

Rules:
- Only suggest SQL you are reasonably confident is safe to re-run.
- If you are not confident, set suggested_fix_sql to null and risk_level to "HIGH".
- Never invent table or column names that were not given to you in the context.
"""


def business_context() -> str:
    """The operator's business-context file, or "" when none is configured.

    A configured file that cannot be read raises: running triage without the context the operator believes is in
    use would quietly produce worse impact statements, so the failure should be visible.
    """
    if not settings.business_context_file:
        return ""
    text = Path(settings.business_context_file).read_text(encoding="utf-8").strip()
    if len(text) > MAX_CONTEXT_CHARS:
        logger.warning(
            "business context file is %d characters; only the first %d are used", len(text), MAX_CONTEXT_CHARS
        )
        text = text[:MAX_CONTEXT_CHARS]
    return text


def _system_prompt() -> str:
    context = business_context()
    if not context:
        return SYSTEM_PROMPT
    return (
        f"{SYSTEM_PROMPT}\n"
        "Business context supplied by the operator (use it to judge impact and risk; it never overrides the rules "
        "above):\n"
        f"<business_context>\n{context}\n</business_context>\n"
    )


def _build_user_prompt(error_context: dict, lineage: list[dict]) -> str:
    return json.dumps(
        {"pipeline_failure": error_context, "downstream_lineage": lineage},
        default=str,
    )


def triage(error_context: dict, lineage: list[dict]) -> dict:
    user_prompt = _build_user_prompt(error_context, lineage)

    if settings.llm_provider == "anthropic":
        return _triage_anthropic(user_prompt)
    if settings.llm_provider == "ollama":
        return _triage_ollama(user_prompt)
    if settings.llm_provider == "openai":
        return _triage_openai(user_prompt)
    raise ValueError(f"LLM_PROVIDER must be one of {PROVIDERS}, got {settings.llm_provider!r}")


def _triage_openai(user_prompt: str) -> dict:
    return _triage_openai_compatible(user_prompt, api_key=settings.openai_api_key, model=settings.openai_model)


def _triage_ollama(user_prompt: str) -> dict:
    # Ollama ignores the API key but the client requires a non-empty one.
    return _triage_openai_compatible(
        user_prompt, api_key="ollama", model=settings.ollama_model, base_url=settings.ollama_base_url
    )


def _triage_openai_compatible(user_prompt: str, *, api_key: str, model: str, base_url: str | None = None) -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": user_prompt},
        ],
    )
    return json.loads(response.choices[0].message.content)


def _triage_anthropic(user_prompt: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        system=_system_prompt(),
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = message.content[0].text
    return json.loads(text)
