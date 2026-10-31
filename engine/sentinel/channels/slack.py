"""Slack channel: one bot token, a channel per spec entry, messages updated in place."""
from typing import Any

from slack_sdk import WebClient

from sentinel.channels.base import ChannelConfigError, IncidentMessage, incident_facts

SEVERITY_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}


class SlackChannel:
    kind = "slack"

    def __init__(self, name: str, options: dict[str, Any], *, interactive: bool = False, client: Any = None):
        token, channel = options.get("token"), options.get("channel")
        if not channel:
            raise ChannelConfigError(f"slack channel '{name}' needs 'channel' (an id like C0123 or #name)")
        if client is None and not token:
            raise ChannelConfigError(f"slack channel '{name}' needs 'token' (for example ${{env:SLACK_BOT_TOKEN}})")
        self.name = name
        self.interactive = interactive
        self._channel = str(channel)
        self._client = client or WebClient(token=token, timeout=10)

    def send(self, message: IncidentMessage) -> str | None:
        response = self._client.chat_postMessage(
            channel=self._channel, text=self._fallback(message), blocks=self._blocks(message)
        )
        # chat.update needs the channel id, which Slack returns even when the spec used a #name.
        return f"{response['channel']}:{response['ts']}"

    def update(self, ref: str, message: IncidentMessage) -> None:
        channel, _, ts = ref.partition(":")
        self._client.chat_update(channel=channel, ts=ts, text=self._fallback(message), blocks=self._blocks(message))

    @staticmethod
    def _fallback(message: IncidentMessage) -> str:
        return f"[{message.incident.severity.value.upper()}] {message.headline}"

    @staticmethod
    def _blocks(message: IncidentMessage) -> list[dict[str, Any]]:
        incident = message.incident
        icon = SEVERITY_ICON.get(incident.severity.value, "⚪")
        facts = {**incident_facts(incident), **message.facts}
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": f"{icon} {message.headline}"[:150]}},
            {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{v}"} for k, v in list(facts.items())[:10]]},
        ]
        if message.body:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": message.body[:2900]}})
        context = f"Incident `{incident.incident_id}` · workspace `{incident.workspace_id}`"
        if message.link:
            context += f" · <{message.link}|open>"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})
        return blocks
