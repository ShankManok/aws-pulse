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
                }
            ]
        }
    """
    signal_data = event.get("signal")
    persona_ids = event.get("persona_ids", [])

    if not signal_data:
        logger.error("missing_signal_data")
        return {"signal": signal_data, "transformations": []}

    persona_table = dynamodb.Table(os.environ.get("PERSONA_TABLE_NAME", Config.PERSONA_TABLE_NAME))
    results = []

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

            # Transform content using Bedrock
            transformed_text = transform_for_persona(signal_data, config_for_transform)

            # Extract recipients from persona members
            members = persona_config.get("members", [])
            recipients = [m.get("principalId") for m in members if m.get("principalId")]

            # Determine delivery channel (email-only for MVP)
            delivery_prefs = persona_config.get("deliveryPreferences", {})
            channels = delivery_prefs.get("channels", ["email"])
            channel = channels[0] if channels else "email"

            results.append({
                "persona_id": persona_id,
                "transformed_content": transformed_text,
                "channel": channel,
                "recipients": recipients,
            })

            logger.info(
                "content_transformed",
                persona_id=persona_id,
                signal_id=signal_data.get("signal_id"),
                recipients_count=len(recipients),
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
