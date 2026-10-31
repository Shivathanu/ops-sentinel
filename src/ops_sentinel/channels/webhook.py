"""Generic webhook channel: POSTs the incident as JSON, optionally signed, to any URL (Teams/Discord relays, ITSM)."""
import hashlib
import hmac
import json
from typing import Any

import httpx

from ops_sentinel.channels.base import ChannelConfigError, IncidentMessage


class WebhookChannel:
    kind = "webhook"

    def __init__(self, name: str, options: dict[str, Any], *, interactive: bool = False, client: Any = None):
        url = options.get("url")
        if not url or not str(url).startswith(("https://", "http://")):
            raise ChannelConfigError(f"webhook channel '{name}' needs 'url' starting with https://")
        self.name = name
        self.interactive = False  # a plain webhook cannot collect a decision
        self._url = str(url)
        self._secret = options.get("secret")
        self._headers = {str(k): str(v) for k, v in (options.get("headers") or {}).items()}
        self._client = client or httpx.Client(timeout=10)

    def send(self, message: IncidentMessage) -> str | None:
        self._post(message)
        return message.incident.incident_id

    def update(self, ref: str, message: IncidentMessage) -> None:
        self._post(message)

    def _post(self, message: IncidentMessage) -> None:
        incident = message.incident
        body = json.dumps(
            {
                "purpose": message.purpose,
                "headline": message.headline,
                "body": message.body,
                "facts": message.facts,
                "link": message.link,
                "incident": incident.model_dump(mode="json"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", **self._headers}
        if self._secret:
            # Receivers verify with HMAC-SHA256 over the raw body, the same scheme GitHub webhooks use.
            headers["X-Sentinel-Signature"] = "sha256=" + hmac.new(self._secret.encode(), body, hashlib.sha256).hexdigest()
        response = self._client.post(self._url, content=body, headers=headers)
        response.raise_for_status()
