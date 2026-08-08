"""Normalize signals from heterogeneous sources into canonical schema."""
from shared.models import SignalEvent, SignalType, Severity, SeverityLevel, SignalContent, SignalContext


def normalize_cloudwatch_alarm(event: dict) -> SignalEvent:
    """Normalize CloudWatch Alarm State Change event."""
    detail = event.get("detail", {})
    state = detail.get("state", {})
    prev_state = detail.get("previousState", {})

    severity_map = {"ALARM": SeverityLevel.HIGH, "OK": SeverityLevel.INFORMATIONAL, "INSUFFICIENT_DATA": SeverityLevel.LOW}

    return SignalEvent(
        source="aws.cloudwatch",
        signal_type=SignalType.INCIDENT if state.get("value") == "ALARM" else SignalType.LIFECYCLE,
        severity=Severity(level=severity_map.get(state.get("value"), SeverityLevel.MEDIUM), score=70 if state.get("value") == "ALARM" else 20),
        content=SignalContent(
            title=f"CloudWatch Alarm: {detail.get('alarmName', 'Unknown')} - {state.get('value')}",
            raw_detail=f"Alarm transitioned from {prev_state.get('value')} to {state.get('value')}. Reason: {state.get('reasonData', '')}",
        ),
        context=SignalContext(
            account_id=event.get("account", ""),
            region=event.get("region", ""),
            resource_arns=[detail.get("alarmArn", "")],
        ),
    )


def normalize_security_hub_finding(event: dict) -> SignalEvent:
    """Normalize Security Hub finding event."""
    findings = event.get("detail", {}).get("findings", [{}])
    finding = findings[0] if findings else {}

    severity_map = {"CRITICAL": SeverityLevel.CRITICAL, "HIGH": SeverityLevel.HIGH, "MEDIUM": SeverityLevel.MEDIUM, "LOW": SeverityLevel.LOW}
    sev_label = finding.get("Severity", {}).get("Label", "MEDIUM")

    resources = finding.get("Resources", [])
    resource_arns = [r.get("Id", "") for r in resources]

    return SignalEvent(
        source="aws.securityhub",
        signal_type=SignalType.FINDING,
        severity=Severity(
            level=severity_map.get(sev_label, SeverityLevel.MEDIUM),
            score=finding.get("Severity", {}).get("Normalized", 50),
        ),
        content=SignalContent(
            title=finding.get("Title", "Security Hub Finding"),
            raw_detail=finding.get("Description", ""),
            structured_data={"finding_id": finding.get("Id"), "product": finding.get("ProductName")},
        ),
        context=SignalContext(
            account_id=finding.get("AwsAccountId", ""),
            region=event.get("region", ""),
            resource_arns=resource_arns,
        ),
    )
