"""
Walks through the CLI-agent flow with the real pieces and no external services.

REAL:      the engine's FastAPI routes (API-key auth, validation), the oclif CLI in cli/bin, and the job and
           fix commands, which really run as shell processes.
SIMULATED: Exasol (an in-memory dict stands in), the LLM triage (canned text) and Slack (a printed card, and a
           "person" who clicks Approve after a few seconds).

Run from the repo root after `cd cli && npm install && npm run build`:
    python scripts/demo_agent_flow.py
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")
for name in ("EXASOL_ADMIN_USER", "EXASOL_ADMIN_PASSWORD", "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SLACK_APPROVER_IDS"):
    os.environ[name] = ""
API_KEY = "demo-key-0123456789"
os.environ["OPS_SENTINEL_API_KEY"] = API_KEY
os.environ["OPS_SENTINEL_API_AUTH_DISABLED"] = "false"

import uvicorn  # noqa: E402

from sentinel import agent_actions, agent_events, main  # noqa: E402

PORT = 8765
CLI = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "cli", "bin", "run.js"))
incidents: dict[int, dict] = {}


def say(who: str, text: str) -> None:
    print(f"  {who:<8} {text}", flush=True)


def step(title: str) -> None:
    print(f"\n=== {title}", flush=True)


# --- stand-ins for Exasol, the LLM and Slack -------------------------------------------------------------------
def open_incident(event):
    incident_id = len(incidents) + 1
    incidents[incident_id] = {"status": "AWAITING_APPROVAL", "event": event, "fix": None}
    say("ENGINE", f"POST /api/events accepted -> incident #{incident_id} (source={event.source}, fix_id={event.fix_id})")
    return incident_id


async def triage_and_notify(incident_id, event):
    fix = agent_actions.format_action(event.fix_id, event.source, event.job) if event.fix_id else None
    incidents[incident_id]["fix"] = fix
    say("ENGINE", "triage: the LLM reads the redacted error and writes the business impact (canned here)")
    say("SLACK", "+----------------------------------------------------------------+")
    say("SLACK", f"| Incident #{incident_id} - AGENT FAILURE ({event.source})")
    say("SLACK", "| Impact:  The nightly export did not finish; finance reports are stale.")
    say("SLACK", f"| Cause:   {event.error.strip().splitlines()[-1][:60]}")
    say("SLACK", f"| Fix:     {fix}")
    say("SLACK", "| [ Approve & Remediate ]   [ Reject ]")
    say("SLACK", "+----------------------------------------------------------------+")


def get_status(incident_id):
    row = incidents.get(incident_id)
    if row is None:
        return None
    return agent_events.EventStatus(incident_id, row["status"], agent_actions.parse_fix_id(row["fix"]))


def record_result(incident_id, result):
    row = incidents.get(incident_id)
    if row is None or row["status"] != "APPROVED":
        return False
    row["status"] = "REMEDIATED" if result.outcome == "SUCCESS" else "FAILED"
    say("ENGINE", f"POST /api/events/{incident_id}/result -> {row['status']}; audit row written as agent:{result.agent}")
    return True


async def idle_watcher() -> None:
    """The engine's database watcher is not part of this flow and has no database here."""


main.watch_loop = idle_watcher
agent_events.open_incident = open_incident
agent_events.triage_and_notify = triage_and_notify
agent_events.get_status = get_status
agent_events.record_result = record_result


def approver() -> None:
    while not incidents or incidents[1]["status"] != "AWAITING_APPROVAL" or incidents[1]["fix"] is None:
        time.sleep(0.1)
    time.sleep(4)
    say("PERSON", "alice (an allow-listed approver) clicks Approve in Slack")
    incidents[1]["status"] = "APPROVED"
    say("ENGINE", "approval recorded (audit row APPROVED); the engine runs nothing itself")


def main_demo() -> int:
    if not os.path.exists(os.path.join(os.path.dirname(CLI), "..", "dist", "src", "commands", "run.js")):
        print("Build the CLI first: cd cli && npm install && npm run build")
        return 1
    # Prime concurrent.futures' lazy atexit registration from the main thread: the first-ever
    # asyncio.to_thread() call imports ThreadPoolExecutor and registers an atexit hook, which raises
    # "can't register atexit after shutdown" (Python 3.13+ on Windows) if that first call happens from
    # the daemon thread running the server below instead.
    import concurrent.futures
    concurrent.futures.ThreadPoolExecutor(max_workers=1).shutdown(wait=True)
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    threading.Thread(target=approver, daemon=True).start()

    work = tempfile.mkdtemp(prefix="sentinel-demo-")
    lock = os.path.join(work, "export.lock")
    open(lock, "w").close()
    # Node scripts rather than a `sh -c '...'` one-liner (cli/runner.ts spawns the job through the
    # platform's default shell -- cmd.exe on Windows -- which can't parse POSIX syntax like `{ }` or `>&2`),
    # matching the cross-platform pattern cli/examples/flaky-job.mjs already uses.
    job_script = os.path.join(work, "job.mjs")
    with open(job_script, "w") as f:
        f.write(
            "import { existsSync } from 'node:fs';\n"
            "if (existsSync(process.argv[2])) {\n"
            "  console.error('ExportError: partition locked by stale run (password=hunter2hunter2)');\n"
            "  process.exit(2);\n"
            "} else {\n"
            "  console.log('export done');\n"
            "}\n"
        )
    fix_script = os.path.join(work, "fix.mjs")
    with open(fix_script, "w") as f:
        f.write(
            "import { existsSync, unlinkSync } from 'node:fs';\n"
            "if (existsSync(process.argv[2])) unlinkSync(process.argv[2]);\n"
        )
    job = f'node "{job_script}" "{lock}"'
    fix = f'node "{fix_script}" "{lock}"'

    step("1. The job fails")
    say("JOB", "nightly export is blocked by a stale lock file (its error even leaks a password)")
    step("2. sentinel run wraps the job; with --wait it stays for the whole incident")
    say("CLI", "sentinel run --name nightly-export --fix-id clear-stale-lock --fix-run 'rm -f ...' --wait -- <job>")
    env = {**os.environ, "SENTINEL_ENGINE_URL": f"http://127.0.0.1:{PORT}", "OPS_SENTINEL_API_KEY": API_KEY}
    proc = subprocess.Popen(
        ["node", CLI, "run", "--name", "nightly-export", "--fix-id", "clear-stale-lock", "--fix-run", fix, "--wait", "--", job],
        cwd=work, env=env, stderr=subprocess.PIPE, text=True,
    )
    for line in proc.stderr:
        say("CLI", line.rstrip())
    code = proc.wait()

    step("3. What the engine saw and stored")
    row = incidents[1]
    say("ENGINE", f"error text received: {row['event'].error.strip()!r}")
    say("ENGINE", f"fix stored for approval: {row['fix']}")
    say("ENGINE", f"the fix command itself was never sent; only its id. final status: {row['status']}")
    step("4. Result")
    say("CLI", f"exit code {code} (0 because the approved fix succeeded; the job's own code was 2)")
    say("LOCK", f"stale lock file still present? {os.path.exists(lock)}")
    return 0 if code == 0 and row["status"] == "REMEDIATED" else 1


if __name__ == "__main__":
    sys.exit(main_demo())
