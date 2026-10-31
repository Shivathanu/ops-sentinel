"""Request authentication for the REST API.

Slack interactions are authenticated separately by their request signature (see main.py).
"""
import hmac
import logging
from pathlib import Path

from fastapi import Header, HTTPException

from sentinel.config import settings
from sentinel.triage_agent import PROVIDERS

logger = logging.getLogger("sentinel.security")


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency that fails closed: no configured key means no access, not open access."""
    if settings.api_auth_disabled:
        return
    if not settings.api_key:
        logger.error("rejecting an API request: OPS_SENTINEL_API_KEY is not set and auth is not disabled")
        raise HTTPException(status_code=503, detail="API authentication is not configured on this server")
    if x_api_key is None or not hmac.compare_digest(x_api_key.encode(), settings.api_key.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


def startup_warnings() -> list[str]:
    """Insecure-but-allowed settings the operator should see in the log at boot."""
    warnings: list[str] = []
    if settings.api_auth_disabled:
        warnings.append("API authentication is disabled (OPS_SENTINEL_API_AUTH_DISABLED); do not expose this server.")
    elif not settings.api_key:
        warnings.append("OPS_SENTINEL_API_KEY is not set; every /api request will be rejected with 503.")
    if not settings.approver_ids():
        warnings.append("SLACK_APPROVER_IDS is empty; nobody can approve or reject incidents.")
    if not settings.exasol_executor_user:
        warnings.append("EXASOL_EXECUTOR_USER is not set; approved remediations will fail until it is configured.")
    if settings.business_context_file and not Path(settings.business_context_file).is_file():
        warnings.append(f"BUSINESS_CONTEXT_FILE {settings.business_context_file!r} does not exist; triage will fail.")
    if settings.llm_provider not in PROVIDERS:
        warnings.append(f"LLM_PROVIDER {settings.llm_provider!r} is not one of {PROVIDERS}; triage will fail.")
    if not settings.exasol_validate_certificate:
        warnings.append("Exasol certificate validation is disabled; use this only with a local self-signed instance.")
    return warnings
