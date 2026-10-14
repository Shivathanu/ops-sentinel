"""
Module B core: turns raw error/lineage context into a plain-language business
impact narrative and a suggested fix, via whichever LLM provider is configured.
"""
import json

from engine.config import settings

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


def _build_user_prompt(error_context: dict, lineage: list[dict]) -> str:
    return json.dumps(
        {"pipeline_failure": error_context, "downstream_lineage": lineage},
        default=str,
    )


def triage(error_context: dict, lineage: list[dict]) -> dict:
    user_prompt = _build_user_prompt(error_context, lineage)

    if settings.llm_provider == "anthropic":
        return _triage_anthropic(user_prompt)
    return _triage_openai(user_prompt)


def _triage_openai(user_prompt: str) -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.chat.completions.create(
        model=settings.openai_model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
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
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = message.content[0].text
    return json.loads(text)
