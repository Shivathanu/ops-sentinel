# ops-sentinel-cli

Bootstraps and runs [Ops Sentinel](../README.md). One install sets up an incident-management project on your machine:
the engine (Docker Compose), a triage model (hosted key or a local model via Ollama), your business context, and
listeners that watch your own automation. A failure is triaged, a person approves in Slack, and only then does a
**pre-approved fix** run.

It runs next to your jobs, as a sidecar or a wrapper. Nothing needs to be installed inside the jobs themselves.

```
your job / log file / HTTP endpoint
        │  failure
        ▼
  sentinel (this CLI) ──► Ops Sentinel engine ──► LLM triage ──► Slack card ──► a person approves
        ▲                                                                            │
        └──────────────── approved fix id (the engine never sends a command) ◄───────┘
        │ runs the fix from YOUR config, then reports the result
        ▼
  engine records it in the audit trail
```

## Why the engine never sends a command

The fix commands live only in your config file. The engine learns a fix's **id**, shows it to the approver, and on
approval tells the agent that id. The agent runs the command it already has for that id, and nothing else. A
prompt-injected error message or a compromised engine therefore cannot make your machine run anything that is not
already in your config.

## Install

```bash
npm install -g ops-sentinel-cli     # after it is published; until then: cd cli && npm install && npm run build && npm link
sentinel --help
```

Needs Node 22 or newer. `sentinel up` also needs Docker with Compose.

## Quick start

```bash
mkdir my-ops && cd my-ops
sentinel init                # asks for model provider and listeners; writes config, .env, compose file
$EDITOR .env                 # add the model key (not needed for ollama), Slack token, approver ids, Exasol password
sentinel doctor              # checks the setup
sentinel up                  # starts the engine (and Ollama, pulling the model, if chosen)
sentinel watch               # runs the listeners until stopped
```

`sentinel init` writes: `sentinel.config.json`, `.env` (owner-only, with a generated API key and webhook secret),
`business-context.md` (read by the triage model with every incident), `docker-compose.yml`, `.gitignore` and a README.
It never overwrites existing files without `--force`. For scripts: `sentinel init --yes --provider ollama --listener command --listener webhook`.

The engine image is `ops-sentinel:latest`. Build it from the repository with `docker build -t ops-sentinel engine`, or pass
`--engine-build <path-to-repo>` to `init` so Compose builds it. The engine needs an Exasol instance; the generated
`.env` points at one on the host (`host.docker.internal`). Commands read `.env` from `--dir` (default: the current
directory); real environment variables win over it.

Try it without any real job: `examples/demo.config.json` watches a job that fails until its approved fix runs.

```bash
cd examples && sentinel watch -c demo.config.json
# a failure is reported, a Slack card appears, approve it, the fix runs, the next check recovers
```

## Wrap one command

```bash
sentinel run --name nightly-export -- ./export.sh --full
```

Output and exit code pass through untouched, so it drops into cron, CI or a Makefile. On failure the failure is
reported to the engine. Add `--wait` to stay until a person decides and any approved fix has run; the exit code is
then 0 if the fix succeeded, otherwise the job's own. Offer a fix on the command line with
`--fix-id clear-lock --fix-run "./unlock.sh"`, or take it from a matching source in the config. Without a config,
set `SENTINEL_ENGINE_URL` and `OPS_SENTINEL_API_KEY`.

## Configuration

`sentinel.config.json`. Paths are relative to the config file. The API key is never in the file.

```json
{
  "engine": { "url": "https://sentinel.example.com", "apiKeyEnv": "OPS_SENTINEL_API_KEY" },
  "pollSeconds": 10,
  "approvalTimeoutMinutes": 60,
  "logFile": ".sentinel/events.jsonl",
  "sources": [
    {
      "name": "nightly-export",
      "type": "command",
      "run": "./export.sh",
      "intervalSeconds": 3600,
      "timeoutSeconds": 300,
      "fix": { "id": "rerun-export", "run": "./export.sh --retry", "maxAttempts": 2, "timeoutSeconds": 600 }
    },
    { "name": "etl-log", "type": "file", "path": "/var/log/etl.log", "match": "ERROR|FATAL" },
    { "name": "crm-api", "type": "http", "url": "https://crm.example.com/health", "expectStatus": [200, 204] }
  ]
}
```

| Source | Failing when | Defaults |
| --- | --- | --- |
| `command` | the command exits non-zero or exceeds `timeoutSeconds` | every 300 s, 120 s timeout |
| `file` | a new line matches the regular expression `match` (the file is followed from its end; rotation is handled) | every 5 s |
| `http` | the request fails or returns a status not in `expectStatus` | every 60 s, 10 s timeout, status 200 |
| `webhook` | another tool POSTs an alert (CI, status page, monitor); each new `key`/`id`/`fingerprint`/`alert_id` is one incident | `127.0.0.1:8787/event`, secret from `SENTINEL_WEBHOOK_SECRET` |

The webhook listener answers 202 only after the engine accepted the incident (502 otherwise, so senders can retry), requires
the secret as `X-Sentinel-Secret` or `Authorization: Bearer` (at least 16 characters, from the environment), caps bodies
at 64 KB, and ignores repeats of a key while its incident is open. It binds to localhost by default; put TLS in front
before exposing it.

A `fix` is optional. Without one, the incident is still reported and triaged, but there is nothing to approve.
`maxAttempts` retries a failing fix with a growing delay before the result is reported as failed. `cwd` can be set on
a source to choose the directory its commands run in.

`engine.url` must be `https://` unless it points at the local machine, because the API key is sent on every
request; set `engine.allowInsecureHttp` to accept plain http elsewhere.

## Commands

| Command | Purpose |
| --- | --- |
| `sentinel init [--yes] [--force] [--provider p] [--listener l]...` | scaffold a project |
| `sentinel up` / `sentinel down` | start or stop the engine with Docker Compose |
| `sentinel doctor [--offline]` | check env, config, Docker, Ollama and engine reachability |
| `sentinel watch [-c file] [--once]` | watch all sources; `--once` checks once and exits 1 if any fail (for cron) |
| `sentinel run [options] -- <command>` | run one command and report it if it fails |
| `sentinel status [-n 20] [--json]` | recent events from the local log |

Exit codes: 0 success, 1 a monitored thing failed or `doctor` found a problem, 2 usage or config error.

## What happens to an incident

1. The first failing check of an episode is reported once. Repeats are not sent while the incident is open.
2. The agent polls the incident. Nothing runs while it is waiting for a decision.
3. On approval it runs the configured fix once, retrying up to `maxAttempts`.
4. It reports the outcome. The engine marks the incident REMEDIATED or FAILED and writes the audit row.
5. Rejection, or no decision within `approvalTimeoutMinutes`, ends tracking and runs nothing.

Every step is also written to the local event log as one JSON object per line.

## Security notes

- **The config file is code.** It holds commands that run on approval, so protect it like a script.
- Error output is scrubbed of common credential shapes (bearer tokens, `password=`, cloud and Slack keys, URL
  credentials, private keys, the engine API key) before it is sent. This is pattern based and not complete. It is
  sent to the engine, an LLM provider and a Slack channel, so keep secrets out of job output anyway.
- The agent only runs the fix whose id matches what its own config declares for that source.
- Anyone holding the engine API key can report events and read incident status. Use one key per environment.

## Develop and publish

```bash
cd cli
npm install
npm test            # builds, then runs the node:test suite
npm pack --dry-run  # shows exactly what would be published
npm publish --access public
```

`prepublishOnly` runs the tests first. Check that the package name is free on npm before the first publish.
