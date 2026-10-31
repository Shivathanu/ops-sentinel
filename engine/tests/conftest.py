"""Keeps the suite independent of a developer's real .env: environment variables outrank the env file, so
blanking these before the package is imported means no test can pick up (or use) real credentials."""
import os

for name in (
    "EXASOL_ADMIN_USER",
    "EXASOL_ADMIN_PASSWORD",
    "EXASOL_EXECUTOR_USER",
    "EXASOL_EXECUTOR_PASSWORD",
    "OPS_SENTINEL_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_APPROVER_IDS",
    "OPS_SENTINEL_SPEC_FILE",
    "EXASOL_MIGRATOR_USER",
    "EXASOL_MIGRATOR_PASSWORD",
):
    os.environ[name] = ""
os.environ["OPS_SENTINEL_API_AUTH_DISABLED"] = "false"  # a bool field cannot parse an empty string
os.environ["EXASOL_VALIDATE_CERTIFICATE"] = "true"
