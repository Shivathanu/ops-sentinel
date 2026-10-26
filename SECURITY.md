# Security Policy

## Supported versions

Only the latest 0.x release receives fixes.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private reporting: open the repository's **Security** tab and
choose **Report a vulnerability**. Include the version, steps to reproduce and the impact you expect.

## Scope and design

Ops Sentinel can run SQL on a database after a person approves it, so the guardrail and the approval path are
the security-critical parts. Reports about bypassing `_validate_fix_sql`, the Slack signature check, or the atomic
approval claim are in scope and welcome.

Known, tracked weaknesses are listed under "Known limitations" in the README and pinned as strict expected-failure
tests. Until they are fixed, do not expose the API to an untrusted network and do not point it at a database
containing data you cannot afford to modify.
