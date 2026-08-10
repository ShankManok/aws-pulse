"""Escalation Handler - checks if delivery was acknowledged and escalates if not."""
import json
import os
from datetime import datetime
from typing import Optional
import boto3
import structlog
from shared.config import Config

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")
sfn_client = boto3.client("stepfunctions")
scheduler_client = boto3.client("scheduler")


def handler(event, context):
    """Check if a delivery was acknowledged; if not, escalate to next persona.

    Input (from EventBridge Scheduler):
        {
            "delivery_id": "del-xxx-persona-ciso-0",
            "signal": { ... original signal data ... },
            "persona_id": "persona-ciso",
            "escalation_chain": ["persona-sre", "persona-cto"],
            "schedule_name": "pulse-esc-del-xxx-..."
        }

    Actions:
        - If delivery IS acknowledged: delete the schedule, do nothing
        - If delivery NOT acknowledged: mark as escalated, trigger workflow for next persona
    """
    delivery_id = event.get("delivery_id")
    signal_data = event.get("signal", {})
    current_persona_id = event.get("persona_id")
    escalation_chain = event.get("escalation_chain", [])
    schedule_name = event.get("schedule_name", "")

    if not delivery_id:
        logger.error("missing_delivery_id")
        return {"statusCode": 400, "escalated": False}

    delivery_table_name = os.environ.get("DELIVERY_TABLE_NAME", Config.DELIVERY_TABLE_NAME)
    table = dynamodb.Table(delivery_table_name)

    # Check if delivery was acknowledged
    try:
        response = table.get_item(Key={"deliveryId": delivery_id})
        record = response.get("Item")
    except Exception as e:
        logger.error("delivery_lookup_failed", delivery_id=delivery_id, error=str(e))
        return {"statusCode": 500, "escalated": False, "error": str(e)}

    if not record:
        logger.warning("delivery_record_not_found", delivery_id=delivery_id)
        return {"statusCode": 404, "escalated": False}

    # If acknowledged, clean up and exit
    if record.get("acknowledgedAt"):
        logger.info("delivery_already_acknowledged", delivery_id=delivery_id)
        _cleanup_schedule(schedule_name)
        return {"statusCode": 200, "escalated": False, "reason": "already_acknowledged"}

    # Not acknowledged — escalate!
    logger.info(
        "escalation_triggered",
        delivery_id=delivery_id,
        persona_id=current_persona_id,
        escalation_chain=escalation_chain,
    )

    # Mark original delivery as escalated
    now = datetime.utcnow().isoformat() + "Z"
    try:
        table.update_item(
            Key={"deliveryId": delivery_id},
            UpdateExpression="SET escalated = :escalated, escalatedAt = :ts",
            ExpressionAttributeValues={":escalated": True, ":ts": now},
        )
    except Exception as e:
        logger.warning("escalation_mark_failed", delivery_id=delivery_id, error=str(e))

    # Determine next persona in escalation chain
    next_persona_id = _get_next_persona(current_persona_id, escalation_chain)

    if not next_persona_id:
        logger.warning("no_more_escalation_targets", delivery_id=delivery_id)
        _cleanup_schedule(schedule_name)
        return {"statusCode": 200, "escalated": True, "reason": "end_of_chain"}

    # Re-invoke persona workflow for the next persona in chain
    workflow_arn = os.environ.get("PERSONA_WORKFLOW_ARN", "")
    if workflow_arn:
        _trigger_escalation_workflow(workflow_arn, signal_data, next_persona_id)

    # Clean up the one-time schedule
    _cleanup_schedule(schedule_name)

    return {
        "statusCode": 200,
        "escalated": True,
        "escalated_to": next_persona_id,
        "delivery_id": delivery_id,
    }


def _get_next_persona(current_persona_id: str, escalation_chain: list) -> Optional[str]:
    """Get the next persona in the escalation chain after the current one."""
    if not escalation_chain:
        return None

    # If current persona is in the chain, get the next one
    try:
        idx = escalation_chain.index(current_persona_id)
        if idx + 1 < len(escalation_chain):
            return escalation_chain[idx + 1]
        # Current persona is last in chain — no more escalation targets
        return None
    except ValueError:
        pass

    # Current persona is not in chain — escalate to the first persona in chain
    for persona_id in escalation_chain:
        if persona_id != current_persona_id:
            return persona_id

    return None


def _trigger_escalation_workflow(workflow_arn: str, signal_data: dict, next_persona_id: str):
    """Start persona workflow targeting a specific persona for escalation."""
    try:
        # Modify audience hint to target only the escalation persona
        signal_data_copy = json.loads(json.dumps(signal_data))
        signal_data_copy["audience_hint"] = {
            "personas": [next_persona_id],
            "escalation_chain": [],  # Don't re-escalate an escalation
            "sla_acknowledge_minutes": 0,
        }
        signal_data_copy["_escalation"] = True

        execution_name = f"esc-{next_persona_id}-{int(datetime.utcnow().timestamp())}"
        execution_name = execution_name[:80].replace(".", "-")

        sfn_client.start_execution(
            stateMachineArn=workflow_arn,
            name=execution_name,
            input=json.dumps({"signal": signal_data_copy}),
        )

        logger.info(
            "escalation_workflow_started",
            persona_id=next_persona_id,
            execution=execution_name,
        )

    except sfn_client.exceptions.ExecutionAlreadyExists:
        logger.warning("escalation_execution_exists", persona_id=next_persona_id)
    except Exception as e:
        logger.error("escalation_workflow_failed", persona_id=next_persona_id, error=str(e))


def _cleanup_schedule(schedule_name: str):
    """Delete the one-time EventBridge Scheduler schedule after it fires."""
    if not schedule_name:
        return
    try:
        scheduler_client.delete_schedule(
            Name=schedule_name,
            GroupName=os.environ.get("SCHEDULER_GROUP_NAME", "pulse-escalations"),
        )
        logger.info("schedule_cleaned_up", schedule_name=schedule_name)
    except scheduler_client.exceptions.ResourceNotFoundException:
        pass  # Already deleted
    except Exception as e:
        logger.warning("schedule_cleanup_failed", schedule_name=schedule_name, error=str(e))
