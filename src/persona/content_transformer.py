"""Content Transformer - generates persona-specific notification text."""
import json
import os
import boto3
import structlog
from shared.bedrock_client import transform_for_persona

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    """Transform signal content for each matched persona."""
    signal_data = event.get("signal")
    persona_ids = event.get("persona_ids", [])

    persona_table = dynamodb.Table(os.environ.get("PERSONA_TABLE_NAME", ""))
    results = []

    for persona_id in persona_ids:
        # Fetch persona config
        response = persona_table.get_item(Key={"personaId": persona_id})
        persona_config = response.get("Item", {})

        if not persona_config:
            logger.warning("persona_not_found", persona_id=persona_id)
            continue

        # Transform content for this persona
        transformed_text = transform_for_persona(signal_data, persona_config)

        results.append({
            "persona_id": persona_id,
            "transformed_content": transformed_text,
            "channel": persona_config.get("deliveryPreferences", {}).get("channels", ["email"])[0],
            "recipients": [m.get("principalId") for m in persona_config.get("members", [])],
        })

        logger.info("content_transformed", persona_id=persona_id, signal_id=signal_data.get("signal_id"))

    return {"transformations": results}
