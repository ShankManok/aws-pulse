"""PagerDuty Webhook Adapter - normalizes PagerDuty incident webhooks to SignalEvent."""
import hashlib
import hmac
import json
import os
import boto3
import structlog
from shared.models import SignalEvent, SignalContent, Severity, SeverityLevel, SignalContext, AudienceHint

logger = structlog.get_logger()
kinesis = boto3.client("kinesis")
dynamodb = boto3.resource("dynamodb")

# PagerDuty urgency → Pulse severity mapping
URGENCY_MAP = {
    "high": SeverityLevel.HIGH,
    "low": SeverityLevel.LOW,
}

SEVERITY_SCORE_MAP = {
    "high": 75,
    "low": 30,
}


def handler(event, context):
    """Handle PagerDuty webhook: POST /v1/webhooks/pagerduty.

    Validates X-PagerDuty-Signature header and normalizes to SignalEvent.
    """
    # Validate signature
    headers = event.get("headers", {})
    body = event.get("body", "")
    signature = headers.get("X-PagerDuty-Signature") or headers.get("x-pagerduty-signature", "")

    if not _validate_signature(body, signature):
        logger.warning("pagerduty_invalid_signature")
        return {"statusCode": 401, "body": json.dumps({"error": "Invalid signature"})}

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON body"})}

    # PagerDuty v3 webhook format: event.data contains the incident
    pd_event = payload.get("event", {})
    event_type = pd_event.get("event_type", "")
    incident = pd_event.get("data", {})

    if not incident:
        return {"statusCode": 200, "body": json.dumps({"message": "No incident data, skipped"})}

    # Normalize to SignalEvent
    signal = _normalize_incident(incident, event_type)

    # Write to Kinesis + DynamoDB (same pattern as publish_handler)
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

    logger.info("pagerduty_signal_ingested", signal_id=signal.signal_id, pd_event_type=event_type)

    return {
        "statusCode": 201,
        "body": json.dumps({"signalId": signal.signal_id, "status": "new"}),
    }


def _validate_signature(body: str, signature: str) -> bool:
    """Validate PagerDuty webhook signature using HMAC-SHA256."""
    secret = os.environ.get("PAGERDUTY_WEBHOOK_SECRET", "")
    if not secret:
        # No secret configured - skip validation in dev
        logger.warning("pagerduty_no_secret_configured")
        return True

    if not signature:
        return False

    # PagerDuty uses v1=<hmac> format
    expected = hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    sig_value = signature.replace("v1=", "")
    return hmac.compare_digest(expected, sig_value)


def _normalize_incident(incident: dict, event_type: str) -> SignalEvent:
    """Convert a PagerDuty incident to a Pulse SignalEvent."""
    urgency = incident.get("urgency", "high")
    service = incident.get("service", {})
    service_name = service.get("summary", "Unknown Service")

    return SignalEvent(
        source="pagerduty",
        signal_type="incident",
        severity=Severity(
            level=URGENCY_MAP.get(urgency, SeverityLevel.MEDIUM),
            score=SEVERITY_SCORE_MAP.get(urgency, 50),
        ),
        content=SignalContent(
            title=incident.get("title", "PagerDuty Incident"),
            raw_detail=incident.get("description", "") or incident.get("summary", ""),
            structured_data={
                "pd_incident_id": incident.get("id", ""),
                "pd_incident_url": incident.get("html_url", ""),
                "pd_service": service_name,
                "pd_event_type": event_type,
                "pd_status": incident.get("status", ""),
            },
        ),
        context=SignalContext(
            resource_arns=[],
            tags={"pagerduty_service": service_name, "pagerduty_urgency": urgency},
        ),
        audience_hint=AudienceHint(
            personas=["sre"],
            escalation_chain=["persona-sre", "persona-cto"],
            sla_acknowledge_minutes=15 if urgency == "high" else 60,
        ),
    )
