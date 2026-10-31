# Security Policy

## Supported versions

Only the latest 0.x release receives fixes.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private reporting: open the repository's **Security** tab and
choose **Report a vulnerability**. Include the version, steps to reproduce and the impact you expect.

## Scope and design

Ops Sentinel can run SQL on a database after a person approves it, so the guardrail and the approval path are
the security-critical parts. Reports about bypassing `validate_fix_sql`, the Slack signature check, or the atomic
approval claim are in scope and welcome.

Known weaknesses are listed under "Known limitations" in the README. Until you have applied `sql/roles.sql`
and set the API key and approver list, do not expose the API to an untrusted network and do not point it at a database
containing data you cannot afford to modify.

## The CLI agent

The agent runs the commands in its own config file, only after the engine reports that a person approved the fix id
that config declares for that source. The engine never sends a command. Anyone holding the engine API key can report
events and read agent incident status, so use one key per environment and rotate it if it leaks. Error output is
scrubbed for common credential shapes before it is sent, but that cannot be complete.
