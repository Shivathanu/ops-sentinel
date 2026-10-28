"""The marker that stands in for a fix the client-side CLI agent will run itself.

For a database incident `INCIDENTS.SUGGESTED_FIX_SQL` holds one SQL statement that the engine runs after approval.
For an incident reported by the CLI agent (cli/) the fix is a command that lives in the client's own config file,
so the engine never learns or runs it: it records only the fix id, in this marker form. The marker is not valid
SQL, so the guardrail would refuse it even if it were ever routed to the SQL path.
"""
import re

SOURCE_TYPE = "CLI_EVENT"
ACTION_PREFIX = "ACTION:"

_FIX_ID = r"[A-Za-z0-9_.-]{1,64}"
FIX_ID_PATTERN = rf"^{_FIX_ID}$"
# Anchored on the exact text format_action writes, so only a marker this module produced is ever accepted.
_MARKER = re.compile(rf"^{ACTION_PREFIX} ({_FIX_ID}) \(agent source=")


def format_action(fix_id: str, source: str, job: str) -> str:
    """Text stored on the incident and shown in the Slack card as the proposed fix."""
    return f"{ACTION_PREFIX} {fix_id} (agent source={source}, job={job})"


def parse_fix_id(suggested_fix: str | None) -> str | None:
    """The fix id inside a marker, or None when the text is anything else (such as SQL)."""
    match = _MARKER.match(suggested_fix or "")
    return match.group(1) if match else None
