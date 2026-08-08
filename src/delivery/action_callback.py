"""Action callback handler - processes Acknowledge/Escalate/Suppress clicks from emails."""
import json
import os
from datetime import datetime
import boto3
import structlog

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    """Handle POST/GET /v1/actions/{deliveryId}/{action} requests."""
    try:
        path_params = event.get("pathParameters", {}) or {}
        delivery_id = path_params.get("deliveryId")
        action = path_params.get("action")

        if not delivery_id or not action:
            return _response(400, {"error": "Missing deliveryId or action"})

        valid_actions = ("acknowledge", "escalate", "suppress")
        if action not in valid_actions:
            return _response(400, {"error": f"Invalid action. Must be one of: {valid_actions}"})

        table = dynamodb.Table(os.environ["DELIVERY_TABLE_NAME"])

        # Update delivery record with the action
        now = datetime.utcnow().isoformat() + "Z"
        update_expr = "SET #action = :action, actionAt = :ts"
        expr_names = {"#action": "feedback"}
        expr_values = {":action": _map_action_to_feedback(action), ":ts": now}

        if action == "acknowledge":
            update_expr += ", acknowledgedAt = :ts"
        elif action == "escalate":
            update_expr += ", escalated = :escalated"
            expr_values[":escalated"] = True

        table.update_item(
            Key={"deliveryId": delivery_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )

        logger.info("action_recorded", delivery_id=delivery_id, action=action)

        # Return user-friendly HTML for GET requests (email link clicks)
        if event.get("httpMethod") == "GET":
            return _html_response(action, delivery_id)

        return _response(200, {"deliveryId": delivery_id, "action": action, "recordedAt": now})

    except Exception as e:
        logger.error("action_callback_failed", error=str(e))
        return _response(500, {"error": "Internal error"})


def _map_action_to_feedback(action: str) -> str:
    """Map action button to feedback enum."""
    mapping = {
        "acknowledge": "useful",
        "escalate": "escalate",
        "suppress": "noise",
    }
    return mapping.get(action, action)


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _html_response(action: str, delivery_id: str) -> dict:
    """Return confirmation HTML for email link clicks."""
    action_labels = {
        "acknowledge": "Acknowledged",
        "escalate": "Escalated",
        "suppress": "Suppressed",
    }
    label = action_labels.get(action, action.title())

    html = f"""
    <html><body style="font-family: Arial, sans-serif; text-align: center; padding: 40px;">
      <h2 style="color: #059669;">&#10003; {label}</h2>
      <p>Your response has been recorded for delivery <code>{delivery_id}</code>.</p>
      <p style="color: #6B7280; font-size: 14px;">You can close this tab.</p>
    </body></html>
    """
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html"},
        "body": html,
    }
