"""Content Transformer - generates persona-specific notification text via Bedrock."""
import json
import os
import boto3
import structlog
from shared.bedrock_client import transform_for_persona
from shared.config import Config

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    """Transform signal content for each matched persona.

    Emits one transformation entry per (persona × channel) combination so the
    downstream Map state can branch delivery by channel type.

    Input (from Step Functions):
        {
            "signal": { ... signal data ... },
            "persona_ids": ["persona-ciso", "persona-sre", "persona-cto"]
        }

    Output:
        {
            "signal": { ... signal data ... },
            "transformations": [
                {
                    "persona_id": "persona-ciso",
                    "transformed_content": "...",
                    "channel": "email",
                    "recipients": ["ciso@example.com"],
                    "escalation_after_minutes": 15,
                    "escalation_chain": ["persona-sre", "persona-cto"]
                },
                {
                    "persona_id": "persona-sre",
                    "transformed_content": "...",
                    "channel": "email",
                    "recipients": ["sre@example.com"],
                    ...
                },
                {
                    "persona_id": "persona-sre",
                    "transformed_content": "...",
                    "channel": "slack",
                    "recipients": ["sre-team-channel"],
                    ...
                }
            ]
        }
    """
    signal_data = event.get("signal")
    persona_ids = event.get("persona_ids", [])

    if not signal_data:
        logger.error("missing_signal_data")
        return {"signal": signal_data, "transformations": []}

    # Check if this is an escalation (don't re-escalate)
    is_escalation = signal_data.get("_escalation", False)

    persona_table = dynamodb.Table(os.environ.get("PERSONA_TABLE_NAME", Config.PERSONA_TABLE_NAME))
    results = []

    # Get escalation chain from original signal audience_hint
    audience_hint = signal_data.get("audience_hint", {})
    escalation_chain = audience_hint.get("escalation_chain", [])

    for persona_id in persona_ids:
        try:
            # Fetch persona config from DynamoDB
            response = persona_table.get_item(Key={"personaId": persona_id})
            persona_config = response.get("Item")

            if not persona_config:
                logger.warning("persona_not_found", persona_id=persona_id)
                continue

            # Build config dict for bedrock_client.transform_for_persona
            config_for_transform = {
                "role_template": persona_config.get("roleTemplate", "sre"),
                "language_level": persona_config.get("languageLevel", "technical_summary"),
            }

            # Transform content using Bedrock (one call per persona, reused across channels)
            transformed_text = transform_for_persona(signal_data, config_for_transform)

            # Extract delivery preferences
            delivery_prefs = persona_config.get("deliveryPreferences", {})
            channels = delivery_prefs.get("channels", ["email"])
            escalation_minutes = int(delivery_prefs.get("escalationAfterMinutes", 0))

            # Extract recipients from persona members
            members = persona_config.get("members", [])
            recipients_by_channel = _group_recipients_by_channel(members, channels)

            # Emit one entry per channel
            for channel in channels:
                channel_recipients = recipients_by_channel.get(channel, [])
                if not channel_recipients:
                    # Fallback: use all members for this channel
                    channel_recipients = [m.get("principalId") for m in members if m.get("principalId")]

                entry = {
                    "persona_id": persona_id,
                    "transformed_content": transformed_text,
                    "channel": channel,
                    "recipients": channel_recipients,
                }

                # Add escalation metadata (only for non-escalation deliveries)
                if not is_escalation and escalation_minutes > 0:
                    entry["escalation_after_minutes"] = escalation_minutes
                    entry["escalation_chain"] = escalation_chain

                results.append(entry)

            logger.info(
                "content_transformed",
                persona_id=persona_id,
                signal_id=signal_data.get("signal_id"),
                channels=channels,
                recipients_count=sum(len(v) for v in recipients_by_channel.values()),
            )

        except Exception as e:
            logger.error(
                "transform_failed",
                persona_id=persona_id,
                signal_id=signal_data.get("signal_id"),
                error=str(e),
            )
            continue

    return {"signal": signal_data, "transformations": results}


def _group_recipients_by_channel(members: list[dict], channels: list[str]) -> dict[str, list[str]]:
    """Group member recipients by their preferred channels.

    If a member has specific channel preferences, route accordingly.
    Otherwise, default to all configured channels.
    """
    result: dict[str, list[str]] = {ch: [] for ch in channels}

    for member in members:
        principal_id = member.get("principalId", "")
        if not principal_id:
            continue

        member_channels = member.get("channels", channels)
        for ch in member_channels:
            if ch in result:
                result[ch].append(principal_id)

    return result
