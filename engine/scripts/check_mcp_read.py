"""
Proves the real MCP read path end to end: spawns the actual
`exasol-mcp-server` the same way exakit configures it for Claude/Codex/
Cursor, over stdio, and calls sentinel.mcp_client's real functions against the
live local Exasol database.

Run: .venv\\Scripts\\python.exe scripts\\check_mcp_read.py
"""
import asyncio

from dotenv import load_dotenv

load_dotenv()

from sentinel.mcp_client import get_error_context, get_lineage_for_table, mcp_session  # noqa: E402


async def main() -> None:
    print("Connecting to the real Exasol MCP server over stdio (uvx exasol-mcp-server)...")
    async with mcp_session() as session:
        tools = await session.list_tools()
        print(f"MCP server exposes {len(tools.tools)} tool(s):")
        for tool in tools.tools:
            print(f"  - {tool.name}")

    print("\nFetching the most recent FAILED pipeline log via MCP (read-only)...")
    # Find the latest injected failure directly to pass its LOG_ID in — the
    # watcher normally does this scan step itself; here we just need a
    # known-good id to prove the MCP read call works.
    from sentinel.db import get_connection

    with get_connection() as conn:
        row = conn.execute(
            "SELECT LOG_ID, PIPELINE_ID, TARGET_TABLE FROM PIPELINE_LOGS "
            "WHERE STATUS = 'FAILED' ORDER BY LOG_ID DESC LIMIT 1"
        ).fetchone()
    if not row:
        print("No FAILED rows found — run scripts/inject_demo_incident.py first.")
        return
    log_id, pipeline_id, target_table = row
    print(f"  (found via admin connection, for test setup only: LOG_ID={log_id})")

    error_context = await get_error_context(pipeline_id, log_id)
    print(f"\nsentinel.mcp_client.get_error_context(...) -> {error_context}")

    lineage = await get_lineage_for_table(target_table)
    print(f"\nsentinel.mcp_client.get_lineage_for_table('{target_table}') -> {lineage}")

    print("\nThis was a real MCP round trip over the read-only mcp_readonly login.")


if __name__ == "__main__":
    asyncio.run(main())
