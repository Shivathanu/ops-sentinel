from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    llm_provider: str = "openai"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"

    # Slack
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_channel_id: str = ""

    # Exasol MCP (read path) — this launches the real `exasol-mcp-server`
    # package via `uvx`, the same way exakit configures it for Claude/Codex/
    # Cursor. See `exakit info` for the DSN and `exakit mcp-status` /
    # each client's config for the exact invocation this mirrors.
    exasol_mcp_command: str = "uvx"
    exasol_mcp_args: str = "exasol-mcp-server@1.10.1"
    exa_dsn: str = "127.0.0.1:8563"
    exa_user: str = "mcp_readonly"
    exa_password: str = ""
    exa_mcp_settings: str = (
        '{"enable_read_query": true, "enable_summarize_table": true, '
        '"enable_query_profiling": true, "enable_write_query": false, '
        '"enable_write_bucketfs": false}'
    )
    exa_ssl_cert_validation: str = "yes"  # set to "no" only for a local self-signed instance

    # Exasol engine connection: incident bookkeeping, audit rows and dashboard aggregation. The EXASOL_ADMIN_*
    # names are kept for compatibility, but this should be the limited `sentinel_engine` user from
    # sql/roles.sql, never SYS.
    exasol_admin_host: str = "127.0.0.1"
    exasol_admin_port: str = "8563"
    exasol_admin_user: str = ""
    exasol_admin_password: str = ""
    exasol_schema: str = "SENTINEL"
    exasol_pool_size: int = 5
    exasol_validate_certificate: bool = True  # set to false only for a local self-signed instance

    # Exasol executor connection: runs approved, guardrail-validated remediation SQL and nothing else. It has
    # data-table privileges only, so it cannot touch INCIDENTS or the audit trail even if the guardrail failed.
    exasol_executor_user: str = ""
    exasol_executor_password: str = ""

    # API authentication for /api/*. Fails closed: with no key and auth not explicitly disabled, the API
    # answers 503.
    api_key: str = Field(default="", validation_alias="OPS_SENTINEL_API_KEY")
    api_auth_disabled: bool = Field(default=False, validation_alias="OPS_SENTINEL_API_AUTH_DISABLED")

    # Comma-separated Slack user IDs (for example U012ABCDEF) allowed to approve or reject. Empty means nobody.
    slack_approver_ids: str = ""

    # Watcher
    watch_interval_seconds: int = 10
    cost_spike_threshold_usd: float = 5.0
    trust_score_window_hours: int = 24

    def approver_ids(self) -> frozenset[str]:
        return frozenset(part.strip() for part in self.slack_approver_ids.split(",") if part.strip())


settings = Settings()
