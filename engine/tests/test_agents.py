import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from sentinel.agents.definitions import BUILTIN, InvestigationResult, resolve_definitions
from sentinel.agents.runtime import SUBMIT, AgentDefinition, AgentRunner, _output_schema
from sentinel.agents.service import render_analysis
from sentinel.agents.tools import Tool, ToolContext, ToolRegistry, render, schema, validate_arguments
from sentinel.agents.toolsets import exasol_toolset
from sentinel.core.spec import AgentSpec, Budget
from sentinel.llm.anthropic_provider import AnthropicProvider
from sentinel.llm.gateway import ModelGateway
from sentinel.llm.openai_provider import OpenAICompatibleProvider
from sentinel.llm.types import AssistantTurn, ModelBinding, Price, ToolCall, ToolResult, ToolSpec, Usage
from tests.fakes import FakeConnection, FakeStatement, factory


class Answer(BaseModel):
    answer: str
    confidence: str


# A scripted model ---------------------------------------------------------------------------------------------------

class ScriptedSession:
    def __init__(self, script: list[AssistantTurn], log: list):
        self.model = "scripted"
        self._script = script
        self.log = log

    def add_user(self, text: str) -> None:
        self.log.append(("user", text))

    def complete(self) -> AssistantTurn:
        turn = self._script.pop(0)
        self.log.append(("assistant", turn))
        return turn

    def add_tool_results(self, results) -> None:
        self.log.append(("results", results))


class ScriptedProvider:
    """Hands out one script per started session, in order (parent first, then subagents as they start)."""

    def __init__(self, *scripts: list[AssistantTurn]):
        self.scripts = list(scripts)
        self.sessions: list[tuple[str, list[ToolSpec], list]] = []

    def start(self, system: str, tools: list[ToolSpec]) -> ScriptedSession:
        log: list = []
        self.sessions.append((system, tools, log))
        return ScriptedSession(self.scripts.pop(0), log)


def turn(*calls: tuple[str, dict], text: str = "", stop: str | None = None, tokens: int = 100) -> AssistantTurn:
    tool_calls = [ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(calls)]
    return AssistantTurn(text, tool_calls, stop or ("tool_use" if calls else "end"), Usage(tokens, tokens // 2), "scripted")


class FixedGateway:
    def __init__(self, provider, price: Price | None = None):
        self.binding = ModelBinding("smart", provider, "scripted", price or Price(1.0, 2.0))

    def resolve(self, role: str) -> ModelBinding:
        return self.binding


def lookup_tool(calls: list) -> Tool:
    def handler(args, ctx):
        calls.append(args)
        return {"status": "replica lag 900s", "host": args["host"]}

    return Tool("lookup", "Look up a host.", schema({"host": {"type": "string"}}, ["host"]), handler, toolset="ops")


def agent(**overrides) -> AgentDefinition:
    base = dict(name="solo", description="d", instructions="Answer.", output_model=Answer, toolsets=("ops",))
    return AgentDefinition(**{**base, **overrides})


CTX = ToolContext("team-a", "INC1", secrets=frozenset({"hunter2-secret"}))


def test_an_agent_calls_tools_then_submits_a_validated_result() -> None:
    calls: list = []
    provider = ScriptedProvider([
        turn(("lookup", {"host": "db1"}), ("lookup", {"host": "db2"})),  # two calls in one turn run in parallel
        turn((SUBMIT, {"answer": "replica lag", "confidence": "high"})),
    ])
    runner = AgentRunner(FixedGateway(provider), ToolRegistry().add(lookup_tool(calls)), {"solo": agent()})

    result = runner.run("solo", "why slow?", CTX)

    assert result.ok and result.output == {"answer": "replica lag", "confidence": "high"}
    assert sorted(c["host"] for c in calls) == ["db1", "db2"]
    assert result.steps == 2 and result.usage.input_tokens == 200
    assert result.cost_usd == pytest.approx((200 * 1.0 + 100 * 2.0) / 1_000_000)
    _, tools, log = provider.sessions[0]
    assert [t.name for t in tools] == ["lookup", SUBMIT]
    results = next(entry[1] for entry in log if entry[0] == "results")
    assert len(results) == 2 and not any(r.is_error for r in results)  # all results returned together


def test_an_invalid_result_is_sent_back_for_correction() -> None:
    provider = ScriptedProvider([
        turn((SUBMIT, {"answer": "x"})),  # missing confidence
        turn((SUBMIT, {"answer": "x", "confidence": "low"})),
    ])
    runner = AgentRunner(FixedGateway(provider), ToolRegistry(), {"solo": agent(toolsets=())})

    result = runner.run("solo", "q", CTX)

    assert result.ok and result.steps == 2
    correction = next(entry[1][0] for entry in provider.sessions[0][2] if entry[0] == "results")
    assert correction.is_error and "confidence" in correction.content


def test_write_tools_unknown_tools_and_bad_arguments_never_run() -> None:
    ran: list = []
    write = Tool("restart", "Restart.", schema({}), lambda a, c: ran.append("restart"), capability="write", toolset="ops")
    provider = ScriptedProvider([
        turn(("restart", {}), ("teleport", {}), ("lookup", {"hostname": "x"})),
        turn((SUBMIT, {"answer": "a", "confidence": "low"})),
    ])
    runner = AgentRunner(FixedGateway(provider), ToolRegistry().add(write, lookup_tool(ran)), {"solo": agent()})

    runner.run("solo", "q", CTX)

    assert ran == []
    results = next(entry[1] for entry in provider.sessions[0][2] if entry[0] == "results")
    assert all(r.is_error for r in results)
    assert "cannot be called directly" in results[0].content
    assert "unknown tool 'teleport'" in results[1].content
    assert "missing required argument(s): host" in results[2].content


def test_tool_output_is_redacted_and_truncated() -> None:
    noisy = Tool("dump", "Dump.", schema({}), lambda a, c: "token=hunter2-secret " + "x" * 50, max_output_chars=30,
                 toolset="ops")
    provider = ScriptedProvider([turn(("dump", {})), turn((SUBMIT, {"answer": "a", "confidence": "low"}))])
    AgentRunner(FixedGateway(provider), ToolRegistry().add(noisy), {"solo": agent()}).run("solo", "q", CTX)

    content = next(entry[1] for entry in provider.sessions[0][2] if entry[0] == "results")[0].content
    assert "hunter2-secret" not in content and "[REDACTED]" in content and "truncated" in content


def test_a_text_answer_gets_one_nudge_and_json_in_text_is_accepted() -> None:
    provider = ScriptedProvider([
        turn(text="I think it's the replica."),
        turn(text='Sure: {"answer": "replica", "confidence": "medium"}'),
    ])
    result = AgentRunner(FixedGateway(provider), ToolRegistry(), {"solo": agent(toolsets=())}).run("solo", "q", CTX)

    assert result.ok and result.output["answer"] == "replica"
    assert any(kind == "user" and SUBMIT in text for kind, text in provider.sessions[0][2][1:] if kind == "user")


def test_the_step_budget_warns_then_stops() -> None:
    grace = turn((SUBMIT, {"answer": "partial: lookups inconclusive", "confidence": "low"}))
    provider = ScriptedProvider([*(turn(("lookup", {"host": "a"})) for _ in range(3)), grace])
    runner = AgentRunner(FixedGateway(provider), ToolRegistry().add(lookup_tool([])),
                         {"solo": agent(budget=Budget(max_steps=3))})

    result = runner.run("solo", "q", CTX)

    # The run is flagged as exhausted but keeps its best partial answer from the grace turn.
    assert result.status == "budget_exhausted" and result.steps == 4 and not result.ok
    assert result.output["answer"].startswith("partial")
    log = provider.sessions[0][2]
    assert any(kind == "user" and "budget is nearly used up" in str(text) for kind, text in log)
    assert any(kind == "user" and "budget for this task is exhausted" in str(text) for kind, text in log)


def test_the_cost_budget_stops_an_expensive_run() -> None:
    provider = ScriptedProvider([turn(("lookup", {"host": "a"}), tokens=1_000_000), turn(text="no idea")])
    runner = AgentRunner(FixedGateway(provider, Price(10.0, 10.0)), ToolRegistry().add(lookup_tool([])),
                         {"solo": agent(budget=Budget(max_cost_usd=5.0))})

    result = runner.run("solo", "q", CTX)

    assert result.status == "budget_exhausted" and result.output is None and "no idea" in result.error


def test_refusals_and_provider_errors_end_the_run_cleanly() -> None:
    refused = ScriptedProvider([turn(stop="refusal")])
    assert AgentRunner(FixedGateway(refused), ToolRegistry(), {"solo": agent()}).run("solo", "q", CTX).status == "refused"

    class Broken:
        def start(self, system, tools):
            raise ConnectionError("model endpoint down")

    result = AgentRunner(FixedGateway(Broken()), ToolRegistry(), {"solo": agent()}).run("solo", "q", CTX)
    assert result.status == "failed" and "model endpoint down" in result.error


def test_subagents_are_tools_with_their_own_context_and_their_spend_counts_for_the_parent() -> None:
    lead = agent(name="lead", toolsets=(), subagents=("helper",))
    helper = agent(name="helper", description="Checks hosts.", toolsets=("ops",))
    provider = ScriptedProvider(
        [turn(("delegate_helper", {"task": "check db1"})), turn((SUBMIT, {"answer": "db1 lag", "confidence": "high"}))],
        [turn(("lookup", {"host": "db1"})), turn((SUBMIT, {"answer": "lagging", "confidence": "high"}))],
    )
    runner = AgentRunner(FixedGateway(provider), ToolRegistry().add(lookup_tool([])), {"lead": lead, "helper": helper})

    result = runner.run("lead", "investigate", CTX)

    assert result.ok
    lead_tools = [t.name for t in provider.sessions[0][1]]
    assert lead_tools == ["delegate_helper", SUBMIT]  # the lead cannot call the helper's tools itself
    assert [t.name for t in provider.sessions[1][1]] == ["lookup", SUBMIT]
    assert result.usage.input_tokens == 400  # 2 own turns + 2 helper turns
    report = json.loads(next(e[1] for e in provider.sessions[0][2] if e[0] == "results")[0].content)
    assert report == {"status": "succeeded", "report": {"answer": "lagging", "confidence": "high"}}


def test_subagent_depth_is_limited() -> None:
    a = agent(name="a", toolsets=(), subagents=("b",))
    b = agent(name="b", toolsets=(), subagents=("a",))
    provider = ScriptedProvider([turn((SUBMIT, {"answer": "x", "confidence": "low"}))])
    AgentRunner(FixedGateway(provider), ToolRegistry(), {"a": a, "b": b}, max_depth=0).run("a", "q", CTX)
    assert [t.name for t in provider.sessions[0][1]] == [SUBMIT]


def test_output_schemas_are_inlined_for_every_provider() -> None:
    schema_ = _output_schema(InvestigationResult)

    assert "$defs" not in json.dumps(schema_) and "$ref" not in json.dumps(schema_)
    assert schema_["properties"]["findings"]["items"]["properties"]["claim"]["type"] == "string"


# Definitions and overrides ------------------------------------------------------------------------------------------

def test_spec_overrides_extend_builtins_and_drop_unconfigured_toolsets() -> None:
    agents = resolve_definitions([AgentSpec(name="dbt_analyst", extends="data_analyst",
                                            instructions="Check dbt first.", model="smart")], {"incident"})

    assert agents["dbt_analyst"].model_role == "smart"
    assert "Check dbt first." in agents["dbt_analyst"].instructions
    assert agents["data_analyst"].toolsets == ()  # exasol is not configured here
    assert set(BUILTIN) <= set(agents)
    with pytest.raises(ValueError, match="must extend"):
        resolve_definitions([AgentSpec(name="ghost")], set())


def test_the_gateway_falls_back_between_roles() -> None:
    from sentinel.core.spec import ModelRole

    built = []
    gateway = ModelGateway({"smart": ModelRole(provider="anthropic", model="claude-opus-5-5")},
                           provider_factory=lambda role: built.append(role.model) or object())

    assert gateway.resolve("fast").role == "smart"
    assert gateway.resolve("fast").price.input == 4.0
    assert built == ["claude-opus-5-5"]  # resolved once, then cached


# Provider adapters --------------------------------------------------------------------------------------------------

def _anthropic_response(content, stop="tool_use"):
    return SimpleNamespace(content=content, stop_reason=stop, model="claude-opus-5-5", stop_details=None,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=90,
                                                 cache_creation_input_tokens=0))


def test_anthropic_adapter_caches_the_prefix_round_trips_content_and_enables_fallbacks() -> None:
    sent: list[dict] = []
    tool_use = SimpleNamespace(type="tool_use", id="tu1", name="lookup", input={"host": "db1"})
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    client = SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: sent.append(kw) or _anthropic_response(
            [thinking, tool_use]))),
        messages=SimpleNamespace(create=lambda **kw: pytest.fail("5.5 models go through the fallback-enabled call")),
    )
    session = AnthropicProvider("claude-opus-5-5", client=client).start(
        "system prompt", [ToolSpec("a", "A", {"type": "object"}), ToolSpec("b", "B", {"type": "object"})])
    session.add_user("go")

    result = session.complete()
    session.add_tool_results([ToolResult("tu1", "ok")])

    request = sent[0]
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" in request["tools"][-1] and "cache_control" not in request["tools"][0]
    assert request["betas"] == ["server-side-fallback-2026-07-01"] and request["extra_body"]["fallbacks"] == "default"
    assert "temperature" not in request and "tool_choice" not in request
    assert result.tool_calls == [ToolCall("tu1", "lookup", {"host": "db1"})]
    assert result.usage.cache_read_tokens == 90
    messages = session._messages
    assert messages[1]["content"][0] is thinking  # thinking block passed back unchanged
    assert messages[2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu1", "content": "ok", "is_error": False}


def test_openai_adapter_echoes_tool_calls_and_reports_bad_arguments() -> None:
    call = SimpleNamespace(id="call1", function=SimpleNamespace(name="lookup", arguments="{not json"))
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="", tool_calls=[call]))],
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=7, prompt_tokens_details=None), model="llama3.1:8b")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))
    session = OpenAICompatibleProvider("llama3.1:8b", client=client).start("sys", [ToolSpec("lookup", "L", {"type": "object"})])
    session.add_user("go")

    result = session.complete()

    assert result.stop_reason == "tool_use"  # local servers sometimes say "stop" with tool calls
    assert result.tool_calls[0].parse_error.startswith("arguments are not valid JSON")
    assert session._messages[-1]["tool_calls"][0]["id"] == "call1"


# Tools --------------------------------------------------------------------------------------------------------------

def test_argument_validation_and_rendering() -> None:
    tool = Tool("t", "T", schema({"n": {"type": "integer"}, "mode": {"type": "string", "enum": ["a", "b"]}}, ["n"]),
                lambda a, c: None)

    assert validate_arguments(tool, {"n": 1, "mode": "a"}) is None
    assert "must be of type integer" in validate_arguments(tool, {"n": True})
    assert "must be one of" in validate_arguments(tool, {"n": 1, "mode": "z"})
    assert "unknown argument" in validate_arguments(tool, {"n": 1, "x": 2})
    assert render({"a": 1}, 100).content.startswith("{")


def test_exasol_query_tool_allows_only_bounded_reads() -> None:
    conn = FakeConnection()
    conn.script(FakeStatement(), FakeStatement(rows=[(1,), (2,), (3,)]))
    query = next(t for t in exasol_toolset(factory(conn), {"schemas": ["SENTINEL"]}) if t.name == "exasol_query")

    rejected = [query.handler({"sql": sql}, CTX) for sql in
                ("DELETE FROM SENTINEL.X", "SELECT 1; DROP TABLE X", "  update t set a=1")]
    assert all(r.is_error for r in rejected) and not conn.executed

    result = query.handler({"sql": "SELECT ID FROM SENTINEL.PIPELINE_LOGS;", "max_rows": 2}, CTX)
    assert result["rows"] == [[1], [2]] and result["truncated"] is True
    assert conn.executed[1][0] == "SELECT * FROM (SELECT ID FROM SENTINEL.PIPELINE_LOGS) LIMIT 3"


def test_exasol_discovery_respects_the_schema_allow_list() -> None:
    tools = {t.name: t for t in exasol_toolset(factory(FakeConnection()), {"schemas": ["SENTINEL"]})}

    assert tools["exasol_describe_table"].handler({"schema": "OPS_SENTINEL", "table": "JOBS"}, CTX).is_error
    assert tools["exasol_list_tables"].handler({"schema": "x; drop"}, CTX).is_error


def test_analysis_renders_for_channels() -> None:
    text = render_analysis({
        "triage": {"business_impact": "Finance reports are stale.", "likely_cause": "warehouse down"},
        "investigation": {"root_cause": "wh01 refused connections", "confidence": "high",
                          "findings": [{"claim": "3 failed runs", "evidence": "PIPELINE_LOGS"}],
                          "recommended_actions": ["restart wh01"], "suggested_action_id": "rerun-export"},
        "runs": [],
    })

    assert "*Impact:* Finance reports are stale." in text
    assert "*Root cause* (high confidence): wh01 refused connections" in text
    assert "`rerun-export`" in text
    assert render_analysis(None) == ""
