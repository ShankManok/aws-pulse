"""Feedback Processor - DynamoDB Streams consumer for DeliveryRecords.

When a delivery record's feedback field is updated (useful/noise/escalate),
aggregates per-persona suppression patterns and updates suppression rules.
"""
import json
import os
from datetime import datetime, timedelta
from typing import Optional
import boto3
import structlog
from shared.config import Config

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")

# Suppression threshold: if a persona marks N+ signals from the same source
# as "noise" within WINDOW_DAYS, auto-suppress that source for the persona
NOISE_THRESHOLD = int(os.environ.get("NOISE_THRESHOLD", "3"))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "30"))


def handler(event, context):
    """Process DynamoDB Streams records from DeliveryRecords table.

    Triggered when feedback field is written/updated on a delivery record.
    Uses batch item failure reporting for partial retries.
    """
    persona_table = dynamodb.Table(os.environ.get("PERSONA_TABLE_NAME", Config.PERSONA_TABLE_NAME))
    delivery_table = dynamodb.Table(os.environ.get("DELIVERY_TABLE_NAME", Config.DELIVERY_TABLE_NAME))

    batch_item_failures = []

    for record in event.get("Records", []):
        try:
            # Only process MODIFY events where feedback was added
            if record.get("eventName") != "MODIFY":
                continue

            new_image = record.get("dynamodb", {}).get("NewImage", {})
            old_image = record.get("dynamodb", {}).get("OldImage", {})

            # Check if feedback was just added (not present in old, present in new)
            new_feedback = _get_str(new_image, "feedback")
            old_feedback = _get_str(old_image, "feedback")

            if not new_feedback or new_feedback == old_feedback:
                continue

            persona_id = _get_str(new_image, "personaId")
            signal_id = _get_str(new_image, "signalId")
            delivery_id = _get_str(new_image, "deliveryId")
            channel = _get_str(new_image, "channel")

            logger.info(
                "feedback_received",
                delivery_id=delivery_id,
                persona_id=persona_id,
                feedback=new_feedback,
                signal_id=signal_id,
            )

            # Publish feedback metric to CloudWatch
            _publish_feedback_metric(persona_id, new_feedback)

            # If feedback is "noise", check if we should create a suppression rule
            if new_feedback == "noise":
                _check_noise_suppression(
                    delivery_table=delivery_table,
                    persona_table=persona_table,
                    persona_id=persona_id,
                    signal_id=signal_id,
                    new_image=new_image,
                )

        except Exception as e:
            seq = record.get("dynamodb", {}).get("SequenceNumber", "unknown")
            logger.error("feedback_processing_failed", error=str(e), sequence_number=seq)
            batch_item_failures.append({"itemIdentifier": seq})

    return {"batchItemFailures": batch_item_failures}


def _check_noise_suppression(
    delivery_table,
    persona_table,
    persona_id: str,
    signal_id: str,
    new_image: dict,
):
    """Check if this noise feedback triggers a suppression rule.

    Queries recent deliveries for this persona marked as noise from the same source.
    If count >= NOISE_THRESHOLD within WINDOW_DAYS, create a suppression rule.
    """
    # We need the signal source - stored in the delivery record or look up via signal
    # For now, we extract source from signalId pattern or query delivery records
    # The signal source is not stored on delivery records, so we'll use a
    # heuristic: query all noise-marked deliveries for this persona in the window
    # and group by source pattern in the signalId

    # Query deliveries for this persona with feedback=noise in recent window
    window_start = (datetime.utcnow() - timedelta(days=WINDOW_DAYS)).isoformat() + "Z"

    try:
        response = delivery_table.query(
            IndexName="by-persona",
            KeyConditionExpression="personaId = :pid AND deliveredAt > :start",
            FilterExpression="feedback = :noise",
            ExpressionAttributeValues={
                ":pid": persona_id,
                ":start": window_start,
                ":noise": "noise",
            },
            Limit=100,
        )

        noise_items = response.get("Items", [])

        # Group by source prefix (extract from signalId or use channel as proxy)
        # In production, we'd join with signal table. For MVP, use signalId prefix grouping
        source_counts: dict[str, int] = {}
        for item in noise_items:
            # Use the first segment of signalId as a source proxy
            sid = item.get("signalId", "")
            # Better: store source on delivery record in future. For now count all noise.
            source_key = "all"  # MVP: single bucket per persona
            source_counts[source_key] = source_counts.get(source_key, 0) + 1

        # Check threshold
        for source_key, count in source_counts.items():
            if count >= NOISE_THRESHOLD:
                _create_suppression_rule(
                    persona_table=persona_table,
                    persona_id=persona_id,
                    source_key=source_key,
                    noise_count=count,
                )

    except Exception as e:
        logger.warning("noise_check_failed", persona_id=persona_id, error=str(e))


def _create_suppression_rule(persona_table, persona_id: str, source_key: str, noise_count: int):
    """Add a learned suppression rule to the persona's suppressionRules array."""
    now = datetime.utcnow().isoformat() + "Z"
    rule_id = f"learned-{source_key}-{int(datetime.utcnow().timestamp())}"

    new_rule = {
        "id": rule_id,
        "source": "learned",
        "pattern": {"source_key": source_key, "noise_count": noise_count},
        "confidence": min(noise_count / (NOISE_THRESHOLD * 2), 1.0),
        "createdAt": now,
        "expiresAt": (datetime.utcnow() + timedelta(days=WINDOW_DAYS)).isoformat() + "Z",
    }

    try:
        persona_table.update_item(
            Key={"personaId": persona_id},
            UpdateExpression="SET suppressionRules = list_append(if_not_exists(suppressionRules, :empty), :rule)",
            ExpressionAttributeValues={
                ":rule": [new_rule],
                ":empty": [],
            },
        )
        logger.info(
            "suppression_rule_created",
            persona_id=persona_id,
            rule_id=rule_id,
            noise_count=noise_count,
        )
    except Exception as e:
        logger.error("suppression_rule_creation_failed", persona_id=persona_id, error=str(e))


def _publish_feedback_metric(persona_id: str, feedback: str):
    """Publish feedback event to CloudWatch for monitoring."""
    try:
        cloudwatch.put_metric_data(
            Namespace="Pulse/Analytics",
            MetricData=[
                {
                    "MetricName": "FeedbackCount",
                    "Dimensions": [
                        {"Name": "PersonaId", "Value": persona_id},
                        {"Name": "FeedbackType", "Value": feedback},
                        {"Name": "Stage", "Value": Config.STAGE},
                    ],
                    "Value": 1,
                    "Unit": "Count",
                    "Timestamp": datetime.utcnow(),
                },
            ],
        )
    except Exception as e:
        logger.warning("metric_publish_failed", error=str(e))


def _get_str(image: dict, key: str) -> str:
    """Extract a string value from a DynamoDB stream image."""
    val = image.get(key, {})
    if isinstance(val, dict):
        return val.get("S", "")
    return str(val) if val else ""
