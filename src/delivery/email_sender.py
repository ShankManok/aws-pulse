"""Email delivery via SES with persona-specific HTML templates."""
import json
import os
import boto3
import structlog

logger = structlog.get_logger()
ses = boto3.client("ses")
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    """Send persona-transformed notification via email."""
    delivery_record = event.get("delivery")
    recipient = delivery_record.get("recipient")
    content = delivery_record.get("transformed_content")
    signal = delivery_record.get("signal")
    persona_id = delivery_record.get("persona_id")

    severity_colors = {"critical": "#DC2626", "high": "#EA580C", "medium": "#CA8A04", "low": "#2563EB", "informational": "#6B7280"}
    severity = signal.get("severity", {}).get("level", "medium")
    color = severity_colors.get(severity, "#6B7280")

    html_body = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: {color}; color: white; padding: 12px 20px; border-radius: 8px 8px 0 0;">
        <strong>{severity.upper()}</strong> - {signal.get('source', 'AWS')}
      </div>
      <div style="border: 1px solid #E5E7EB; border-top: none; padding: 20px; border-radius: 0 0 8px 8px;">
        <h2 style="margin-top: 0;">{signal.get('content', {}).get('title', 'Signal')}</h2>
        <p>{content}</p>
        <hr style="border: none; border-top: 1px solid #E5E7EB; margin: 16px 0;">
        <div style="display: flex; gap: 8px;">
          <a href="#ack" style="background: #059669; color: white; padding: 8px 16px; border-radius: 4px; text-decoration: none;">Acknowledge</a>
          <a href="#escalate" style="background: #DC2626; color: white; padding: 8px 16px; border-radius: 4px; text-decoration: none;">Escalate</a>
          <a href="#suppress" style="background: #6B7280; color: white; padding: 8px 16px; border-radius: 4px; text-decoration: none;">Suppress</a>
        </div>
        <p style="font-size: 12px; color: #9CA3AF; margin-top: 16px;">AWS Pulse | {signal.get('context', {}).get('account_id', '')} | {signal.get('context', {}).get('region', '')}</p>
      </div>
    </body></html>
    """

    try:
        ses.send_email(
            Source=f"Pulse <noreply@{os.environ['SES_DOMAIN']}>",
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {"Data": f"[{severity.upper()}] {signal.get('content', {}).get('title', 'Signal')}"},
                "Body": {"Html": {"Data": html_body}},
            },
        )

        # Record delivery
        table = dynamodb.Table(os.environ.get("DELIVERY_TABLE_NAME", ""))
        table.put_item(Item={
            "deliveryId": f"del-{signal.get('signal_id')}-{persona_id}",
            "signalId": signal.get("signal_id"),
            "personaId": persona_id,
            "recipientId": recipient,
            "channel": "email",
            "deliveredAt": signal.get("ingested_at"),
            "escalated": False,
        })

        logger.info("email_sent", recipient=recipient, signal_id=signal.get("signal_id"))
        return {"statusCode": 200, "delivered": True}

    except Exception as e:
        logger.error("email_failed", error=str(e), recipient=recipient)
        return {"statusCode": 500, "delivered": False, "error": str(e)}
