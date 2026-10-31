import asyncio
import json
from types import SimpleNamespace

import pytest

from sentinel import mcp_client


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _result(*texts: str | None) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(text=text) for text in texts])


def test_sql_tool_is_found_by_name_pattern() -> None:
    tools = [_tool("list_schemas"), _tool("Execute_Query"), _tool("describe_table")]

    assert mcp_client._find_sql_tool_name(tools) == "Execute_Query"


def test_missing_sql_tool_raises_with_remediation_hint() -> None:
    with pytest.raises(RuntimeError, match="No SQL-execution tool"):
        mcp_client._find_sql_tool_name([_tool("list_schemas")])


def test_tool_result_rows_are_read_from_list_and_rows_payloads() -> None:
    result = _result(json.dumps([{"A": 1}]), json.dumps({"rows": [{"A": 2}]}))

    assert mcp_client._parse_tool_result(result) == [{"A": 1}, {"A": 2}]


def test_tool_result_skips_blocks_without_parseable_json() -> None:
    result = _result(None, "not json", json.dumps([{"A": 3}]))

    assert mcp_client._parse_tool_result(result) == [{"A": 3}]


def test_read_path_refuses_non_select_before_spawning_the_mcp_server() -> None:
    with pytest.raises(ValueError, match="only accepts SELECT"):
        asyncio.run(mcp_client.run_readonly_sql("DELETE FROM sales_clean"))


def test_string_literals_double_embedded_quotes() -> None:
    assert mcp_client._sql_string_literal("o'brien") == "'o''brien'"


def test_lineage_query_escapes_a_hostile_table_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    async def fake_run(sql: str) -> list[dict]:
        captured.append(sql)
        return []

    monkeypatch.setattr(mcp_client, "run_readonly_sql", fake_run)

    asyncio.run(mcp_client.get_lineage_for_table("x' OR '1'='1"))

    assert "SOURCE_TABLE = 'x'' OR ''1''=''1'" in captured[0]
