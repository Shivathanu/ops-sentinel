"""Builds and sends the Slack Block Kit Approve/Reject card (Module C, chat half)."""
from slack_sdk import WebClient

from sentinel.config import settings

client = WebClient(token=settings.slack_bot_token)


def posting_token_error(token: str) -> str | None:
    """Why this token cannot call chat.postMessage, or None when it is a bot token.

    An app-level token (xapp-, used for Socket Mode) is rejected by Slack with
    not_allowed_token_type. Posting needs the Bot User OAuth Token (xoxb-).
    """
    if token.startswith("xoxb-"):
        return None
    if token.startswith("xapp-"):
        return (
            "SLACK_BOT_TOKEN is an app-level token (xapp-). Slack rejects chat.postMessage "
            "with not_allowed_token_type. Use the Bot User OAuth Token (xoxb-) from the app's "
            "OAuth & Permissions page."
        )
    if not token:
        return "SLACK_BOT_TOKEN is empty."
    return (
        "SLACK_BOT_TOKEN is not a bot token (xoxb-). Slack rejects other token types "
        "on chat.postMessage with not_allowed_token_type."
    )


def post_incident_card(incident_id: int, source_type: str, business_impact: str,
                        root_cause: str, suggested_fix_sql: str | None,
                        risk_level: str) -> str:
    """Posts the triage result as an interactive card. Returns the Slack message ts
    so it can be stored on the INCIDENTS row and later updated in place."""
    problem = posting_token_error(settings.slack_bot_token)
    if problem:
        raise RuntimeError(problem)
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🚨 Incident #{incident_id} — {source_type}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Business impact:*\n{business_impact}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Root cause:*\n{root_cause}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Suggested fix:*\n```{suggested_fix_sql or 'No automated fix available'}```",
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Risk level: *{risk_level}*"}],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve & Remediate"},
                    "style": "primary",
                    "action_id": "approve_incident",
                    "value": str(incident_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject"},
                    "style": "danger",
                    "action_id": "reject_incident",
                    "value": str(incident_id),
                },
            ],
        },
    ]
    response = client.chat_postMessage(
        channel=settings.slack_channel_id,
        blocks=blocks,
        text=f"Incident #{incident_id} needs your approval",
    )
    return response["ts"]


def update_incident_card(message_ts: str, outcome_text: str) -> None:
    """Replaces the action buttons with a plain outcome line once resolved."""
    problem = posting_token_error(settings.slack_bot_token)
    if problem:
        raise RuntimeError(problem)
    client.chat_update(
        channel=settings.slack_channel_id,
        ts=message_ts,
        text=outcome_text,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": outcome_text}}],
    )
