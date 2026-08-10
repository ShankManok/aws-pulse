"""Datadog Webhook Adapter - normalizes Datadog alert webhooks to SignalEvent."""
import json
import os
import boto3
import structlog
from shared.models import SignalEvent, SignalContent, Severity, SeverityLevel, SignalContext, AudienceHint

logger = structlog.get_logger()
kinesis = boto3.client("kinesis")
dynamodb = boto3.resource("dynamodb")

# Datadog priority → Pulse severity mapping
PRIORITY_MAP = {
    "P1": SeverityLevel.CRITICAL,
    "P2": SeverityLevel.HIGH,
    "P3": SeverityLevel.MEDIUM,
    "P4": SeverityLevel.LOW,
    "P5": SeverityLevel.INFORMATIONAL,
}

PRIORITY_SCORE_MAP = {
    "P1": 95,
    "P2": 75,
    "P3": 50,
    "P4": 30,
    "P5": 10,
}


def handler(event, context):
    """Handle Datadog webhook: POST /v1/webhooks/datadog.

    Validates DD-API-KEY header and normalizes to SignalEvent.
    """
    headers = event.get("headers", {})
    body = event.get("body", "")

    # Validate API key
    api_key = headers.get("DD-API-KEY") or headers.get("dd-api-key", "")
    if not _validate_api_key(api_key):
        logger.warning("datadog_invalid_api_key")
        return {"statusCode": 401, "body": json.dumps({"error": "Invalid API key"})}

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON body"})}

    # Normalize to SignalEvent
    signal = _normalize_alert(payload)

    # Write to Kinesis + DynamoDB
    stream_name = os.environ.get("SIGNAL_STREAM_NAME", "")
    table_name = os.environ.get("SIGNAL_TABLE_NAME", "")

    signal_dict = signal.to_dynamo()

    if stream_name:
        kinesis.put_record(
            StreamName=stream_name,
            Data=json.dumps(signal_dict),
            PartitionKey=signal.context.account_id or signal.signal_id,
        )

    if table_name:
        table = dynamodb.Table(table_name)
        table.put_item(Item=signal_dict)

    logger.info("datadog_signal_ingested", signal_id=signal.signal_id, alert_type=payload.get("alert_type"))

    return {
        "statusCode": 201,
        "body": json.dumps({"signalId": signal.signal_id, "status": "new"}),
    }


def _validate_api_key(api_key: str) -> bool:
    """Validate Datadog webhook API key."""
    expected_key = os.environ.get("DATADOG_WEBHOOK_API_KEY", "")
    if not expected_key:
        logger.warning("datadog_no_api_key_configured")
        return True  # Skip validation in dev

    if not api_key:
        return False

    return api_key == expected_key


def _normalize_alert(payload: dict) -> SignalEvent:
    """Convert a Datadog alert webhook to a Pulse SignalEvent."""
    priority = payload.get("priority", "P3")
    tags = payload.get("tags", [])
    alert_type = payload.get("alert_type", "metric_alert")

    # Parse tags into dict
    tag_dict: dict[str, str] = {}
    resource_arns: list[str] = []
    for tag in tags:
        if ":" in tag:
            key, value = tag.split(":", 1)
            tag_dict[key] = value
            # If tag looks like an ARN, add to resource_arns
            if value.startswith("arn:aws:"):
                resource_arns.append(value)

    # Map alert_type to signal_type
    signal_type = "incident"
    if alert_type in ("metric_alert", "anomaly"):
        signal_type = "incident"
    elif alert_type == "recommendation":
        signal_type = "recommendation"

    return SignalEvent(
        source="datadog",
        signal_type=signal_type,
        severity=Severity(
            level=PRIORITY_MAP.get(priority, SeverityLevel.MEDIUM),
            score=PRIORITY_SCORE_MAP.get(priority, 50),
        ),
        content=SignalContent(
            title=payload.get("title", "Datadog Alert"),
            raw_detail=payload.get("body", "") or payload.get("text", ""),
            structured_data={
                "dd_alert_id": payload.get("alert_id", ""),
                "dd_alert_type": alert_type,
                "dd_monitor_id": str(payload.get("monitor_id", "")),
                "dd_priority": priority,
                "dd_status": payload.get("alert_transition", ""),
                "dd_url": payload.get("link", ""),
            },
        ),
        context=SignalContext(
            account_id=tag_dict.get("aws_account", ""),
            region=tag_dict.get("region", ""),
            resource_arns=resource_arns,
            tags=tag_dict,
        ),
        audience_hint=AudienceHint(
            personas=["sre"],
            escalation_chain=["persona-sre", "persona-cto"],
            sla_acknowledge_minutes=10 if priority in ("P1", "P2") else 30,
        ),
    )
