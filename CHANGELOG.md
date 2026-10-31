# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/) and the
project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `ops-sentinel-cli` (`cli/`): an oclif CLI that bootstraps and runs Ops Sentinel. `sentinel init` scaffolds a project (config, generated API key and webhook secret in a 0600 `.env`, business context, Docker Compose for the engine and an optional local Ollama model); `up`/`down` manage the engine; `doctor` checks the setup; `watch`/`run`/`status` run the listeners. Listeners: command, log file, HTTP check and an authenticated webhook receiver. Failures are reported to the engine, and a pre-approved fix from the client's own config runs once a person approves it.
- Engine: `LLM_PROVIDER=ollama` for a local small model, `BUSINESS_CONTEXT_FILE` injected into the triage prompt, `OPS_SENTINEL_WATCHER_ENABLED` to run for CLI incidents only, and a `Dockerfile`.
- Engine endpoints `POST /api/events`, `GET /api/events/{id}` and `POST /api/events/{id}/result`, and `CLI_EVENT` incidents (`agent_events.py`, `agent_actions.py`).

### Security
- Rewrote the remediation guardrail (`guardrail.py`): masks literals, rejects comments (a commented-out `WHERE` no longer passes), allows one statement, blocks Sentinel and system tables, and restricts targets to the incident's tables.
- `/api/*` requires `X-API-Key`; Slack approvals are limited to `SLACK_APPROVER_IDS`; both fail closed.
- Remediation runs as a separate least-privilege executor user; added `sql/roles.sql`.
- Exasol TLS certificate validation is on by default.
- Escaped the table name interpolated into the lineage query; fixed a connection-pool slot leak.
- Removed `CALL` from the allowed verbs (behavior change).
- Blocked statements are now recorded in the audit table with outcome `BLOCKED`.

### Added
- Apache-2.0 license.
- `pyproject.toml` packaging with `dashboard` and `dev` extras, and a `src/ops_sentinel` layout.
- Unit tests for the remediation guardrail, triage routing, MCP result parsing and the API.
- GitHub Actions workflow running ruff and pytest on Python 3.10 and 3.12.
- Architecture, contributing and security documents.

### Changed
- Renamed the `engine` package to `ops_sentinel` and `db/` to `sql/`.
- Renamed `scripts/test_*.py` to `scripts/check_*.py` because they need live infrastructure and must not be collected by pytest.
- `scoring.recent_incidents` now fails loudly if a row does not match the expected columns.

### Removed
- Committed `engine_run.log` and per-folder requirements files, replaced by `pyproject.toml`.
