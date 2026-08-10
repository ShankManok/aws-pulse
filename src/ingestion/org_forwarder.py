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

    # Normalize to SignalEvent
    signal = _normalize_event(source, detail_type, detail, account_id, region, event_time)

    if not signal:
        logger.info("event_skipped", source=source, detail_type=detail_type)
        return {"statusCode": 200, "processed": False}

    # Publish to Kinesis + DynamoDB
    stream_name = os.environ.get("SIGNAL_STREAM_NAME", Config.SIGNAL_STREAM_NAME)
    signal_table_name = os.environ.get("SIGNAL_TABLE_NAME", Config.SIGNAL_TABLE_NAME)

    signal_dict = signal.to_dynamo()

    if stream_name:
        kinesis.put_record(
            StreamName=stream_name,
            Data=json.dumps(signal_dict),
            PartitionKey=account_id or signal.signal_id,
        )

    if signal_table_name:
        table = dynamodb.Table(signal_table_name)
        table.put_item(Item=signal_dict)

    logger.info(
        "cross_account_signal_published",
        signal_id=signal.signal_id,
        source=source,
        account_id=account_id,
    )

    return {"statusCode": 201, "processed": True, "signalId": signal.signal_id}


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
            resource_arns=resource_arns,
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
