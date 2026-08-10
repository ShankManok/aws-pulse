"""Schedule Escalation - creates EventBridge Scheduler one-time schedules for delivery SLA checks."""
import json
import os
from datetime import datetime, timedelta
import boto3
import structlog
from shared.config import Config

logger = structlog.get_logger()
scheduler_client = boto3.client("scheduler")


def handler(event, context):
    """Create an EventBridge Scheduler one-time schedule for escalation checking.

    Input (from Step Functions, after delivery):
        {
            "delivery": {
                "persona_id": "persona-ciso",
                "transformed_content": "...",
                "channel": "email",
                "recipients": ["ciso@example.com"],
                "escalation_after_minutes": 15,
                "escalation_chain": ["persona-sre", "persona-cto"]
            },
            "signal": { ... signal data ... },
            "delivery_ids": ["del-xxx-persona-ciso-0"]
        }

    Output:
        {
            "statusCode": 200,
            "scheduled": true,
            "schedule_name": "pulse-esc-del-xxx-...",
            "fire_at": "2025-01-01T00:15:00Z"
        }
    """
    delivery = event.get("delivery", {})
    signal_data = event.get("signal", {})
    delivery_ids = event.get("delivery_ids", [])

    persona_id = delivery.get("persona_id", "unknown")
    escalation_minutes = delivery.get("escalation_after_minutes", 0)
    escalation_chain = delivery.get("escalation_chain", [])

    # Skip scheduling if no escalation configured or no delivery IDs
    if not escalation_minutes or escalation_minutes <= 0:
        logger.info("no_escalation_configured", persona_id=persona_id)
        return {"statusCode": 200, "scheduled": False, "reason": "no_escalation_time"}

    if not delivery_ids:
        logger.info("no_delivery_ids", persona_id=persona_id)
        return {"statusCode": 200, "scheduled": False, "reason": "no_deliveries"}

    if not escalation_chain:
        logger.info("no_escalation_chain", persona_id=persona_id)
        return {"statusCode": 200, "scheduled": False, "reason": "no_chain"}

    # Schedule one escalation check per delivery ID
    scheduler_role_arn = os.environ.get("SCHEDULER_ROLE_ARN", "")
    escalation_fn_arn = os.environ.get("ESCALATION_FUNCTION_ARN", "")
    scheduler_group = os.environ.get("SCHEDULER_GROUP_NAME", "pulse-escalations")

    now = datetime.utcnow()
    fire_at = now + timedelta(minutes=escalation_minutes)
    fire_at_str = fire_at.strftime("%Y-%m-%dT%H:%M:%S")

    scheduled_names = []

    for delivery_id in delivery_ids:
        schedule_name = f"pulse-esc-{delivery_id}"
        # EventBridge Scheduler names: max 64 chars, [a-zA-Z0-9-_.]
        schedule_name = schedule_name[:64].replace("@", "-").replace(" ", "-")

        payload = {
            "delivery_id": delivery_id,
            "signal": signal_data,
            "persona_id": persona_id,
            "escalation_chain": escalation_chain,
            "schedule_name": schedule_name,
        }

        try:
            scheduler_client.create_schedule(
                Name=schedule_name,
                GroupName=scheduler_group,
                ScheduleExpression=f"at({fire_at_str})",
                ScheduleExpressionTimezone="UTC",
                FlexibleTimeWindow={"Mode": "OFF"},
                Target={
                    "Arn": escalation_fn_arn,
                    "RoleArn": scheduler_role_arn,
                    "Input": json.dumps(payload),
                },
                ActionAfterCompletion="DELETE",
            )

            scheduled_names.append(schedule_name)
            logger.info(
                "escalation_scheduled",
                delivery_id=delivery_id,
                persona_id=persona_id,
                fire_at=fire_at_str,
                schedule_name=schedule_name,
            )

        except scheduler_client.exceptions.ConflictException:
            logger.warning("schedule_already_exists", schedule_name=schedule_name)
            scheduled_names.append(schedule_name)
        except Exception as e:
            logger.error(
                "schedule_creation_failed",
                delivery_id=delivery_id,
                error=str(e),
            )

    return {
        "statusCode": 200,
        "scheduled": len(scheduled_names) > 0,
        "schedule_names": scheduled_names,
        "fire_at": fire_at.isoformat() + "Z",
    }
