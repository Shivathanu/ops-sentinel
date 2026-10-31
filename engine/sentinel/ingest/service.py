"""Authenticates and ingests one push delivery: body → Signals → incidents (with their jobs) in Exasol."""
import hmac
from collections.abc import Mapping

from sentinel.core.spec import SourceSpec
from sentinel.ingest.sources import SourceError
from sentinel.platform import Platform
from sentinel.store.incidents import RecordedSignal

MAX_BODY_BYTES = 1024 * 1024


class IngestFailure(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def presented_token(headers: Mapping[str, str]) -> str | None:
    direct = headers.get("x-sentinel-token") or headers.get("x-api-key")
    if direct:
        return direct
    auth = headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def authorize(source: SourceSpec, headers: Mapping[str, str], api_key: str, auth_disabled: bool) -> None:
    """A source with its own `token` accepts only that token, so one leaked sender credential cannot post to
    other sources. A source without one accepts the engine API key."""
    expected = source.options.get("token") or api_key
    if auth_disabled and not source.options.get("token"):
        return
    if not expected:
        raise IngestFailure(503, "ingest authentication is not configured (set the source 'token' or OPS_SENTINEL_API_KEY)")
    token = presented_token(headers)
    if token is None or not hmac.compare_digest(token.encode(), str(expected).encode()):
        raise IngestFailure(401, "invalid or missing token")


def ingest(
    platform: Platform,
    workspace: str,
    source_name: str,
    body: bytes,
    headers: Mapping[str, str],
    *,
    api_key: str,
    auth_disabled: bool = False,
) -> list[RecordedSignal]:
    headers = {k.lower(): v for k, v in headers.items()}
    entry = platform.workspace(workspace)
    # Unknown workspace and unknown source answer the same 404, so the endpoint does not reveal which exist.
    source = entry.loaded.spec.source(source_name) if entry else None
    if entry is None or source is None:
        raise IngestFailure(404, "unknown workspace or source")
    authorize(source, headers, api_key, auth_disabled)
    if len(body) > MAX_BODY_BYTES:
        raise IngestFailure(413, "body too large")
    plugin = platform.sources.get(source.type)
    if plugin is None:
        raise IngestFailure(422, f"source type '{source.type}' cannot receive pushes")
    try:
        signals = plugin.parse(source, body, headers)
    except SourceError as exc:
        raise IngestFailure(400, str(exc)) from exc
    environment = entry.loaded.spec.environment
    return [platform.store.record_signal(workspace, environment, signal) for signal in signals]
