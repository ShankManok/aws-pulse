"""Slack delivery via AWS Chatbot with interactive notification messages."""
import json
import os
from datetime import datetime
import boto3
import structlog
from shared.config import Config
from shared.action_tokens import new_token, action_url

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")
sns_client = boto3.client("sns")

SEVERITY_EMOJI = {
    "critical": ":rotating_light:",
    "high": ":warning:",
    "medium": ":large_orange_diamond:",
    "low": ":information_source:",
    "informational": ":memo:",
}

SEVERITY_COLORS = {
    "critical": "#DC2626",
    "high": "#EA580C",
    "medium": "#CA8A04",
    "low": "#2563EB",
    "informational": "#6B7280",
}


def handler(event, context):
    """Send persona-transformed notification to Slack via AWS Chatbot (SNS → Chatbot).

    AWS Chatbot listens on an SNS topic and forwards messages to configured Slack channels.
    We publish a rich JSON message to the SNS topic that Chatbot formats as a Slack message.

    Input (from Step Functions Map state):
        {
            "delivery": {
                "persona_id": "persona-sre",
                "transformed_content": "...",
                "channel": "slack",
                "recipients": ["sre-team-slack-channel"]
            },
            "signal": { ... signal data ... }
        }

    Output:
        {
            "statusCode": 200,
            "delivered": true,
            "delivery_ids": ["del-xxx-persona-sre-slack-0"]
        }
    """
    delivery = event.get("delivery", {})
    signal = event.get("signal", {})

    recipients = delivery.get("recipients", [])
    content = delivery.get("transformed_content", "")
    persona_id = delivery.get("persona_id", "unknown")

    if not recipients:
        logger.warning("no_slack_recipients", persona_id=persona_id)
        return {"statusCode": 200, "delivered": False, "delivery_ids": [], "error": "No recipients"}

    signal_id = signal.get("signal_id", "unknown")
    severity = signal.get("severity", {})
    severity_level = severity.get("level", "medium")
    title = signal.get("content", {}).get("title", "AWS Pulse Signal")
    source = signal.get("source", "AWS")
    account_id = signal.get("context", {}).get("account_id", "")
    region = signal.get("context", {}).get("region", "")

    sns_topic_arn = os.environ.get("CHATBOT_SNS_TOPIC_ARN", "")
    delivery_table_name = os.environ.get("DELIVERY_TABLE_NAME", Config.DELIVERY_TABLE_NAME)
    callback_base = os.environ.get("CALLBACK_API_URL", "").rstrip("/")
    table = dynamodb.Table(delivery_table_name)

    delivery_ids = []
    failed = []

    for idx, recipient in enumerate(recipients):
        delivery_id = f"del-{signal_id}-{persona_id}-slack-{idx}"

        token, token_hash = new_token()
        # Build Slack-formatted message via SNS
        slack_message = _build_slack_message(
            severity_level=severity_level,
            source=source,
            title=title,
            content=content,
            account_id=account_id,
            region=region,
            delivery_id=delivery_id,
            action_token=token,
            callback_base=callback_base,
        )

        try:
            # Publish to SNS topic (Chatbot picks it up and routes to Slack)
            sns_client.publish(
                TopicArn=sns_topic_arn,
                Message=json.dumps(slack_message),
                Subject=f"[{severity_level.upper()}] {title}"[:100],
                MessageAttributes={
                    "severity": {
                        "DataType": "String",
                        "StringValue": severity_level,
                    },
                    "persona": {
                        "DataType": "String",
                        "StringValue": persona_id,
                    },
                },
            )

            # Record delivery for audit trail
            now = datetime.utcnow().isoformat() + "Z"
            table.put_item(Item={
                "deliveryId": delivery_id,
                "actionTokenHash": token_hash,
                "actionTokenExpiresAt": int(datetime.utcnow().timestamp()) + 86400,
                "signalId": signal_id,
                "signalSource": source,
                "personaId": persona_id,
                "recipientId": recipient,
                "channel": "slack",
                "contentVersion": str(hash(content))[:12],
                "deliveredAt": now,
                "escalated": False,
            })

            delivery_ids.append(delivery_id)
            logger.info("slack_sent", recipient=recipient, signal_id=signal_id, persona_id=persona_id)

        except Exception as e:
            logger.error("slack_failed", error=str(e), recipient=recipient, signal_id=signal_id)
            failed.append({"recipient": recipient, "error": str(e)})

    if failed:
        raise RuntimeError(f"Delivery failed for {len(failed)} recipient(s)")

    return {
        "statusCode": 200,
        "delivered": len(delivery_ids) > 0,
        "delivery_ids": delivery_ids,
        "failed": failed,
    }


def _build_slack_message(
    severity_level: str,
    source: str,
    title: str,
    content: str,
    account_id: str,
    region: str,
    delivery_id: str,
    callback_base: str,
    action_token: str = "",
) -> dict:
    """Build a Slack Block Kit message payload for AWS Chatbot."""
    emoji = SEVERITY_EMOJI.get(severity_level, ":bell:")

    ack_url = action_url(callback_base, delivery_id, "acknowledge", action_token)
    escalate_url = action_url(callback_base, delivery_id, "escalate", action_token)
    suppress_url = action_url(callback_base, delivery_id, "suppress", action_token)

    return {
        "version": "1.0",
        "source": "custom",
        "content": {
            "textType": "client-markdown",
            "title": f"{emoji} [{severity_level.upper()}] {title}",
            "description": content,
            "nextSteps": [
                f"<{ack_url}|:white_check_mark: Acknowledge>",
                f"<{escalate_url}|:arrow_up: Escalate>",
                f"<{suppress_url}|:no_bell: Suppress>",
            ],
            "keywords": [
                f"Source: {source}",
                f"Account: {account_id}",
                f"Region: {region}",
                f"Severity: {severity_level}",
            ],
        },
    }
