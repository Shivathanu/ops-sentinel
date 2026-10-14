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
    exa_ssl_cert_validation: str = "no"

    # Exasol admin (write path — remediation only)
    exasol_admin_host: str = "127.0.0.1"
    exasol_admin_port: str = "8563"
    exasol_admin_user: str = "sys"
    exasol_admin_password: str = ""
    exasol_schema: str = "SENTINEL"
    exasol_pool_size: int = 5

    # Watcher
    watch_interval_seconds: int = 10
    cost_spike_threshold_usd: float = 5.0
    trust_score_window_hours: int = 24


settings = Settings()
