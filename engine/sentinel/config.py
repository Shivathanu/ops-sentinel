from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # The repo-root .env (when run from engine/) and a local .env; later files win, so a local .env overrides.
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    # LLM: "openai", "anthropic", or "ollama" for a local small model served by Ollama
    llm_provider: str = "openai"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"
    ollama_base_url: str = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible endpoint
    ollama_model: str = "llama3.2"

    # Optional file describing the business (critical processes, owners, what must never be touched). It is added
    # to every triage prompt so the model can phrase impact in the operator's own terms.
    business_context_file: str = ""

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

    # Set false to run the engine for agent-reported incidents only, with no polling of PIPELINE_LOGS/QUERY_METRICS.
    watcher_enabled: bool = Field(default=True, validation_alias="OPS_SENTINEL_WATCHER_ENABLED")

    # Platform (v2). Platform tables live in their own schema so the v1 SENTINEL schema, which also holds the demo
    # business tables, is never touched by migrations.
    platform_schema: str = Field(default="OPS_SENTINEL", validation_alias="OPS_SENTINEL_PLATFORM_SCHEMA")
    # Optional schema owner used only to apply migrations and install UDFs; falls back to EXASOL_ADMIN_* when unset.
    exasol_migrator_user: str = ""
    exasol_migrator_password: str = ""
    # Path to the workspace spec (sentinel.yaml). Empty means no spec: v1 behaviour only.
    spec_file: str = Field(default="", validation_alias="OPS_SENTINEL_SPEC_FILE")
    # Apply pending platform migrations and reinstall UDFs when the api starts. Safe with several replicas; turn off
    # where migrations run as a separate step (the Helm pre-upgrade Job).
    auto_migrate: bool = Field(default=True, validation_alias="OPS_SENTINEL_AUTO_MIGRATE")
    worker_poll_seconds: float = Field(default=2.0, validation_alias="OPS_SENTINEL_WORKER_POLL_SECONDS")
    worker_lease_seconds: int = Field(default=300, validation_alias="OPS_SENTINEL_WORKER_LEASE_SECONDS")

    # Watcher
    watch_interval_seconds: int = 10
    cost_spike_threshold_usd: float = 5.0
    trust_score_window_hours: int = 24

    def approver_ids(self) -> frozenset[str]:
        return frozenset(part.strip() for part in self.slack_approver_ids.split(",") if part.strip())


settings = Settings()
