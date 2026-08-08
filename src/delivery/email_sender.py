"""Email delivery via SES with persona-specific HTML templates."""
import json
import os
from datetime import datetime
import boto3
import structlog
from shared.config import Config

logger = structlog.get_logger()
ses = boto3.client("ses")
dynamodb = boto3.resource("dynamodb")

SEVERITY_COLORS = {
    "critical": "#DC2626",
    "high": "#EA580C",
    "medium": "#CA8A04",
    "low": "#2563EB",
    "informational": "#6B7280",
}


def handler(event, context):
    """Send persona-transformed notification via email.

    Input (from Step Functions Map state):
        {
            "delivery": {
                "persona_id": "persona-ciso",
                "transformed_content": "...",
                "channel": "email",
                "recipients": ["ciso@example.com"]
            },
            "signal": { ... signal data ... }
        }

    Output:
        {
            "statusCode": 200,
            "delivered": true,
            "delivery_ids": ["del-xxx-persona-ciso-0"]
        }
    """
    delivery = event.get("delivery", {})
    signal = event.get("signal", {})

    recipients = delivery.get("recipients", [])
    content = delivery.get("transformed_content", "")
    persona_id = delivery.get("persona_id", "unknown")

    if not recipients:
        logger.warning("no_recipients", persona_id=persona_id)
        return {"statusCode": 200, "delivered": False, "error": "No recipients"}

    signal_id = signal.get("signal_id", "unknown")
    severity = signal.get("severity", {})
    severity_level = severity.get("level", "medium")
    color = SEVERITY_COLORS.get(severity_level, "#6B7280")
    title = signal.get("content", {}).get("title", "AWS Pulse Signal")
    source = signal.get("source", "AWS")
    account_id = signal.get("context", {}).get("account_id", "")
    region = signal.get("context", {}).get("region", "")

    ses_domain = os.environ.get("SES_DOMAIN", "pulse.example.com")
    delivery_table_name = os.environ.get("DELIVERY_TABLE_NAME", Config.DELIVERY_TABLE_NAME)
    table = dynamodb.Table(delivery_table_name)

    delivery_ids = []
    failed = []

    for idx, recipient in enumerate(recipients):
        delivery_id = f"del-{signal_id}-{persona_id}-{idx}"

        html_body = _build_html(
            severity_level=severity_level,
            color=color,
            source=source,
            title=title,
            content=content,
            account_id=account_id,
            region=region,
            delivery_id=delivery_id,
        )

        try:
            ses.send_email(
                Source=f"AWS Pulse <noreply@{ses_domain}>",
                Destination={"ToAddresses": [recipient]},
                Message={
                    "Subject": {"Data": f"[{severity_level.upper()}] {title}"},
                    "Body": {"Html": {"Data": html_body}},
                },
            )

            # Record delivery for audit trail
            now = datetime.utcnow().isoformat() + "Z"
            table.put_item(Item={
                "deliveryId": delivery_id,
                "signalId": signal_id,
                "personaId": persona_id,
                "recipientId": recipient,
                "channel": "email",
                "contentVersion": str(hash(content))[:12],
                "deliveredAt": now,
                "escalated": False,
            })

            delivery_ids.append(delivery_id)
            logger.info("email_sent", recipient=recipient, signal_id=signal_id, persona_id=persona_id)

        except Exception as e:
            logger.error("email_failed", error=str(e), recipient=recipient, signal_id=signal_id)
            failed.append({"recipient": recipient, "error": str(e)})

    return {
        "statusCode": 200,
        "delivered": len(delivery_ids) > 0,
        "delivery_ids": delivery_ids,
        "failed": failed,
    }


def _build_html(
    severity_level: str,
    color: str,
    source: str,
    title: str,
    content: str,
    account_id: str,
    region: str,
    delivery_id: str,
) -> str:
    """Build the notification email HTML with action buttons."""
    # Action callback base URL (set via env or default)
    callback_base = os.environ.get("CALLBACK_API_URL", "")

    ack_url = f"{callback_base}/v1/actions/{delivery_id}/acknowledge"
    escalate_url = f"{callback_base}/v1/actions/{delivery_id}/escalate"
    suppress_url = f"{callback_base}/v1/actions/{delivery_id}/suppress"

    return f"""<!DOCTYPE html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; background: #F9FAFB; padding: 20px;">
  <div style="background: {color}; color: white; padding: 14px 20px; border-radius: 8px 8px 0 0; font-size: 14px;">
    <strong>{severity_level.upper()}</strong> &mdash; {source}
  </div>
  <div style="background: white; border: 1px solid #E5E7EB; border-top: none; padding: 24px; border-radius: 0 0 8px 8px;">
    <h2 style="margin: 0 0 12px 0; font-size: 18px; color: #111827;">{title}</h2>
    <p style="color: #374151; line-height: 1.6; margin: 0 0 20px 0;">{content}</p>
    <hr style="border: none; border-top: 1px solid #E5E7EB; margin: 20px 0;">
    <div>
      <a href="{ack_url}" style="display: inline-block; background: #059669; color: white; padding: 10px 18px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500; margin-right: 8px;">&#10003; Acknowledge</a>
      <a href="{escalate_url}" style="display: inline-block; background: #DC2626; color: white; padding: 10px 18px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500; margin-right: 8px;">&#9650; Escalate</a>
      <a href="{suppress_url}" style="display: inline-block; background: #6B7280; color: white; padding: 10px 18px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500;">&#10005; Suppress</a>
    </div>
    <p style="font-size: 12px; color: #9CA3AF; margin: 20px 0 0 0;">
      AWS Pulse | Account: {account_id} | Region: {region} | ID: {delivery_id}
    </p>
  </div>
</body></html>"""
