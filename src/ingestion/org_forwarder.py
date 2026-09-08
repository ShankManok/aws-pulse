"""Org Forwarder - cross-account EventBridge event normalization.

Deployed in member accounts to forward EventBridge events to the central
Pulse account. Normalizes events from different accounts into SignalEvent format.

In the central account, this Lambda is also used to process cross-account
events received on the default event bus.
"""
import json
import os
from datetime import datetime
import boto3
import structlog
from shared.models import (
    SignalEvent, SignalContent, Severity, SeverityLevel,
    SignalContext, AudienceHint, SignalType,
)
from shared.config import Config
from shared.ingest import persist
try:
    from normalizer import normalize_cloudwatch_alarm, normalize_security_hub_finding
except ModuleNotFoundError:
    from ingestion.normalizer import normalize_cloudwatch_alarm, normalize_security_hub_finding

logger = structlog.get_logger()
kinesis = boto3.client("kinesis")
dynamodb = boto3.resource("dynamodb")

# EventBridge source → signal type mapping
SOURCE_TYPE_MAP = {
    "aws.cloudwatch": SignalType.INCIDENT,
    "aws.securityhub": SignalType.FINDING,
    "aws.health": SignalType.INCIDENT,
    "aws.guardduty": SignalType.FINDING,
    "aws.config": SignalType.FINDING,
}

# EventBridge source → severity defaults
SOURCE_SEVERITY_MAP = {
    "aws.guardduty": SeverityLevel.HIGH,
    "aws.securityhub": SeverityLevel.MEDIUM,
    "aws.health": SeverityLevel.HIGH,
    "aws.cloudwatch": SeverityLevel.MEDIUM,
    "aws.config": SeverityLevel.LOW,
}


def handler(event, context):
    """Process cross-account EventBridge events.

    Input: EventBridge event (detail-type varies by source)

    Normalizes to SignalEvent and publishes to Kinesis for processing.
    """
    source = event.get("source", "")
    detail_type = event.get("detail-type", "")
    detail = event.get("detail", {})
    account_id = event.get("account", "")
    region = event.get("region", "")
    event_time = event.get("time", datetime.utcnow().isoformat() + "Z")

    logger.info(
        "cross_account_event_received",
        source=source,
        detail_type=detail_type,
        account_id=account_id,
        region=region,
    )

    # Security Hub batches can contain multiple independent findings.
    if source == "aws.securityhub" and len(detail.get("findings", [])) > 1:
        results = [handler({**event, "detail": {**detail, "findings": [finding]}}, context)
                   for finding in detail["findings"]]
        return {"statusCode": 201, "processed": True, "signalIds": [r["signalId"] for r in results]}
    if source == "aws.cloudwatch" and detail_type == "CloudWatch Alarm State Change":
        signal = normalize_cloudwatch_alarm(event)
    elif source == "aws.securityhub":
        signal = normalize_security_hub_finding(event)
    else:
        signal = _normalize_event(source, detail_type, detail, account_id, region, event_time)
    signal.context.tags["cross_account"] = str(account_id != os.environ.get("AWS_ACCOUNT_ID", "")).lower()
    if event.get("resources"):
        signal.context.resource_arns = list(set(signal.context.resource_arns + event["resources"]))

    # Publish to Kinesis + DynamoDB
    signal_table_name = os.environ.get("SIGNAL_TABLE_NAME", Config.SIGNAL_TABLE_NAME)

    finding = detail.get('findings', [{}])[0] if source == 'aws.securityhub' else {}
    event_key = f"{event['id']}:{finding.get('Id', finding.get('Title', ''))}" if event.get('id') else None
    signal_id = persist(signal, dynamodb.Table(signal_table_name), event_key)

    logger.info(
        "cross_account_signal_published",
        signal_id=signal.signal_id,
        source=source,
        account_id=account_id,
    )

    return {"statusCode": 201, "processed": True, "signalId": signal_id}


def _normalize_event(
    source: str,
    detail_type: str,
    detail: dict,
    account_id: str,
    region: str,
    event_time: str,
) -> SignalEvent:
    """Normalize an EventBridge event to a SignalEvent."""
    signal_type = SOURCE_TYPE_MAP.get(source, SignalType.INCIDENT)
    default_severity = SOURCE_SEVERITY_MAP.get(source, SeverityLevel.MEDIUM)

    # Extract resource ARNs from the event
    resource_arns = _extract_resource_arns(source, detail)

    # Extract title and detail
    title, raw_detail = _extract_content(source, detail_type, detail)

    # Extract severity if available
    severity_level, severity_score = _extract_severity(source, detail, default_severity)

    return SignalEvent(
        source=source,
        signal_type=signal_type,
        severity=Severity(
            level=severity_level,
            score=severity_score,
        ),
        content=SignalContent(
            title=title,
            raw_detail=raw_detail,
            structured_data={
                "event_source": source,
                "detail_type": detail_type,
                "original_account": account_id,
                "original_region": region,
                "event_time": event_time,
            },
        ),
        context=SignalContext(
            account_id=account_id,
            region=region,
            resource_arns=[arn.replace("ec2:::instance/", f"ec2:{region}:{account_id}:instance/") for arn in resource_arns],
            tags={"cross_account": "true"},
        ),
        audience_hint=AudienceHint(
            personas=_default_personas_for_source(source),
            escalation_chain=["persona-sre", "persona-cto"],
            sla_acknowledge_minutes=30,
        ),
    )


def _extract_resource_arns(source: str, detail: dict) -> list[str]:
    """Extract resource ARNs from various EventBridge event formats."""
    arns = []

    # CloudWatch alarm
    if "alarmArn" in detail:
        arns.append(detail["alarmArn"])

    # Security Hub findings
    if "findings" in detail:
        for finding in detail.get("findings", []):
            for resource in finding.get("Resources", []):
                arn = resource.get("Id", "")
                if arn.startswith("arn:"):
                    arns.append(arn)

    # GuardDuty
    if "resource" in detail:
        resource = detail["resource"]
        if "instanceDetails" in resource:
            instance_id = resource["instanceDetails"].get("instanceId", "")
            if instance_id:
                arns.append(f"arn:aws:ec2:::instance/{instance_id}")

    # Health events
    if "affectedEntities" in detail:
        for entity in detail.get("affectedEntities", []):
            arn = entity.get("entityValue", "")
            if arn.startswith("arn:"):
                arns.append(arn)

    return arns


def _extract_content(source: str, detail_type: str, detail: dict) -> tuple:
    """Extract title and detail text from the event."""
    if source == "aws.cloudwatch":
        alarm_name = detail.get("alarmName", detail.get("configuration", {}).get("description", "CloudWatch Alarm"))
        state = detail.get("state", {}).get("value", "ALARM")
        return f"CloudWatch: {alarm_name} ({state})", json.dumps(detail.get("state", {}))

    elif source == "aws.securityhub":
        findings = detail.get("findings", [])
        if findings:
            title = findings[0].get("Title", "Security Hub Finding")
            desc = findings[0].get("Description", "")
            return title, desc
        return "Security Hub Finding", detail_type

    elif source == "aws.health":
        service = detail.get("service", "AWS")
        event_type = detail.get("eventTypeCode", "unknown")
        desc = detail.get("eventDescription", [{}])
        desc_text = desc[0].get("latestDescription", "") if desc else ""
        return f"AWS Health: {service} - {event_type}", desc_text

    elif source == "aws.guardduty":
        title = detail.get("title", "GuardDuty Finding")
        desc = detail.get("description", "")
        return title, desc

    return f"{source}: {detail_type}", json.dumps(detail)[:500]


def _extract_severity(source: str, detail: dict, default: SeverityLevel) -> tuple:
    """Extract severity from event-specific fields."""
    if source == "aws.securityhub":
        findings = detail.get("findings", [])
        if findings:
            sev = findings[0].get("Severity", {})
            label = sev.get("Label", "MEDIUM").lower()
            normalized = sev.get("Normalized", 50)
            level_map = {"critical": SeverityLevel.CRITICAL, "high": SeverityLevel.HIGH,
                         "medium": SeverityLevel.MEDIUM, "low": SeverityLevel.LOW,
                         "informational": SeverityLevel.INFORMATIONAL}
            return level_map.get(label, default), min(normalized, 100)

    if source == "aws.guardduty":
        gd_severity = detail.get("severity", 5)
        if gd_severity >= 7:
            return SeverityLevel.HIGH, 80
        elif gd_severity >= 4:
            return SeverityLevel.MEDIUM, 55
        else:
            return SeverityLevel.LOW, 30

    return default, 50


def _default_personas_for_source(source: str) -> list[str]:
    """Determine default personas based on event source."""
    if source in ("aws.securityhub", "aws.guardduty"):
        return ["ciso", "sre"]
    elif source == "aws.health":
        return ["sre", "cto"]
    return ["sre"]
