"""PagerDuty Webhook Adapter - normalizes PagerDuty incident webhooks to SignalEvent."""
import hmac
import json
import os
from shared.ingest import persist
from shared.webhooks import secret
from shared.runtime import body as decode_body
import hashlib
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
    headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        import base64
        try:
            body = base64.b64decode(body, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return {"statusCode": 400, "body": json.dumps({"error": "Invalid encoded body"})}
    signature = headers.get("X-PagerDuty-Signature") or headers.get("x-pagerduty-signature", "")

    if not _validate_signature(body, signature):
        logger.warning("pagerduty_invalid_signature")
        return {"statusCode": 401, "body": json.dumps({"error": "Invalid signature"})}

    try:
        payload = decode_body(event)
    except (ValueError, TypeError, UnicodeDecodeError):
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON body"})}

    # PagerDuty v3 webhook format: event.data contains the incident
    try:
        pd_event = payload.get("event", {})
        event_type = pd_event.get("event_type", "")
        incident = pd_event.get("data", {})

        if not incident:
            return {"statusCode": 200, "body": json.dumps({"message": "No incident data, skipped"})}

        # Normalize to SignalEvent
        signal = _normalize_incident(incident, event_type)
    except (ValueError, TypeError, AttributeError):
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid provider payload"})}

    # Persist atomically; the DynamoDB outbox forwards to Kinesis
    table_name = os.environ.get("SIGNAL_TABLE_NAME", "")

    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    sid = persist(signal, dynamodb.Table(table_name), f"pagerduty:{key}" if key else None)

    logger.info("pagerduty_signal_ingested", signal_id=signal.signal_id, pd_event_type=event_type)

    return {
        "statusCode": 201,
        "body": json.dumps({"signalId": sid, "status": "new"}),
    }


def _validate_signature(body: str, signature: str) -> bool:
    """Validate PagerDuty webhook signature using HMAC-SHA256."""
    signing_secret = secret("PAGERDUTY_WEBHOOK_SECRET")
    if not signing_secret:
        # An unconfigured integration must reject incoming requests.
        logger.warning("pagerduty_no_secret_configured")
        return False

    if not signature:
        return False

    # PagerDuty uses v1=<hmac> format
    expected = hmac.new(
        signing_secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return any(hmac.compare_digest(expected, value.strip()[3:]) for value in signature.split(",") if value.strip().startswith("v1="))


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
