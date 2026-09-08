"""ServiceNow Webhook Adapter - normalizes ServiceNow incident webhooks to SignalEvent."""
import base64
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

# ServiceNow impact + urgency → Pulse severity mapping
# ServiceNow uses 1=High, 2=Medium, 3=Low for both impact and urgency
# Combined priority: impact × urgency gives effective severity
SEVERITY_MATRIX = {
    (1, 1): SeverityLevel.CRITICAL,   # High impact + High urgency
    (1, 2): SeverityLevel.HIGH,        # High impact + Medium urgency
    (1, 3): SeverityLevel.MEDIUM,      # High impact + Low urgency
    (2, 1): SeverityLevel.HIGH,        # Medium impact + High urgency
    (2, 2): SeverityLevel.MEDIUM,      # Medium impact + Medium urgency
    (2, 3): SeverityLevel.LOW,         # Medium impact + Low urgency
    (3, 1): SeverityLevel.MEDIUM,      # Low impact + High urgency
    (3, 2): SeverityLevel.LOW,         # Low impact + Medium urgency
    (3, 3): SeverityLevel.INFORMATIONAL,  # Low impact + Low urgency
}

SCORE_MATRIX = {
    (1, 1): 95, (1, 2): 80, (1, 3): 60,
    (2, 1): 75, (2, 2): 50, (2, 3): 35,
    (3, 1): 55, (3, 2): 30, (3, 3): 10,
}


def handler(event, context):
    """Handle ServiceNow webhook: POST /v1/webhooks/servicenow.

    Validates Basic Auth credentials and normalizes to SignalEvent.
    """
    headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}

    # Validate Basic Auth
    auth_header = headers.get("Authorization") or headers.get("authorization", "")
    if not _validate_basic_auth(auth_header):
        logger.warning("servicenow_invalid_auth")
        return {"statusCode": 401, "body": json.dumps({"error": "Invalid credentials"})}

    try:
        payload = decode_body(event)
    except (ValueError, TypeError, UnicodeDecodeError):
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON body"})}

    # Normalize to SignalEvent
    try:
        signal = _normalize_incident(payload)
    except (ValueError, TypeError, AttributeError):
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid provider payload"})}

    # Persist atomically; the DynamoDB outbox forwards to Kinesis
    table_name = os.environ.get("SIGNAL_TABLE_NAME", "")

    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    sid = persist(signal, dynamodb.Table(table_name), f"servicenow:{key}" if key else None)

    logger.info("servicenow_signal_ingested", signal_id=signal.signal_id, number=payload.get("number"))

    return {
        "statusCode": 201,
        "body": json.dumps({"signalId": sid, "status": "new"}),
    }


def _validate_basic_auth(auth_header: str) -> bool:
    """Validate Basic Auth credentials."""
    expected_user = secret("SERVICENOW_WEBHOOK_USER")
    expected_pass = secret("SERVICENOW_WEBHOOK_PASS")

    if not expected_user or not expected_pass:
        logger.warning("servicenow_no_auth_configured")
        return False

    if not auth_header or not auth_header.startswith("Basic "):
        return False

    try:
        encoded = auth_header.replace("Basic ", "")
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
        return hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_pass)
    except (ValueError, UnicodeDecodeError):
        return False


def _normalize_incident(payload: dict) -> SignalEvent:
    """Convert a ServiceNow incident to a Pulse SignalEvent."""
    # ServiceNow uses numeric impact/urgency (1=High, 2=Medium, 3=Low)
    impact = int(payload.get("impact", 2))
    urgency = int(payload.get("urgency", 2))

    # Clamp to valid range
    impact = max(1, min(3, impact))
    urgency = max(1, min(3, urgency))

    severity_key = (impact, urgency)
    severity_level = SEVERITY_MATRIX.get(severity_key, SeverityLevel.MEDIUM)
    severity_score = SCORE_MATRIX.get(severity_key, 50)

    # Determine signal type from ServiceNow category
    category = payload.get("category", "").lower()
    signal_type = "incident"
    if "security" in category:
        signal_type = "finding"
    elif "change" in category or "request" in category:
        signal_type = "lifecycle"

    # Map assignment group to persona hint
    assignment_group = payload.get("assignment_group", "")
    personas = ["sre"]  # default
    if "security" in assignment_group.lower():
        personas = ["ciso"]
    elif "executive" in assignment_group.lower() or "management" in assignment_group.lower():
        personas = ["cto"]

    return SignalEvent(
        source="servicenow",
        signal_type=signal_type,
        severity=Severity(
            level=severity_level,
            score=severity_score,
        ),
        content=SignalContent(
            title=payload.get("short_description", "ServiceNow Incident"),
            raw_detail=payload.get("description", ""),
            structured_data={
                "snow_number": payload.get("number", ""),
                "snow_sys_id": payload.get("sys_id", ""),
                "snow_state": payload.get("state", ""),
                "snow_category": payload.get("category", ""),
                "snow_assignment_group": assignment_group,
                "snow_priority": str(payload.get("priority", "")),
                "snow_impact": impact,
                "snow_urgency": urgency,
            },
        ),
        context=SignalContext(
            tags={
                "servicenow_number": payload.get("number", ""),
                "servicenow_category": payload.get("category", ""),
            },
        ),
        audience_hint=AudienceHint(
            personas=personas,
            escalation_chain=["persona-sre", "persona-cto"],
            sla_acknowledge_minutes=15 if severity_level in (SeverityLevel.CRITICAL, SeverityLevel.HIGH) else 60,
        ),
    )
