"""Delivery-scoped feedback links with explicit confirmation and replay protection."""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from html import escape
from urllib.parse import parse_qs

import boto3
import structlog
from botocore.exceptions import ClientError

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    params = event.get("pathParameters") or {}
    delivery_id, action = params.get("deliveryId"), params.get("action")
    if not delivery_id or action not in ("acknowledge", "escalate", "suppress"):
        return _response(400, {"error": "Invalid deliveryId or action"})
    method = event.get("httpMethod", "POST")
    if method not in ("GET", "POST"):
        return _response(405, {"error": "Method not allowed"})
    token = (event.get("queryStringParameters") or {}).get("token", "")
    if method == "POST" and not token:
        try:
            body = event.get("body") or ""
            token = parse_qs(body).get("token", [""])[0]
        except (TypeError, ValueError):
            return _response(400, {"error": "Invalid request body"})
    if not isinstance(token, str) or not token:
        return _response(403, {"error": "Invalid or expired action link"})
    digest = hashlib.sha256(token.encode()).hexdigest()
    try:
        table = dynamodb.Table(os.environ["DELIVERY_TABLE_NAME"])
        record = table.get_item(Key={"deliveryId": delivery_id}, ConsistentRead=True).get("Item", {})
        expected = record.get("actionTokenHash", "")
        if (not expected or not hmac.compare_digest(expected, digest)
                or int(record.get("actionTokenExpiresAt", 0)) <= time.time()):
            return _response(403, {"error": "Invalid or expired action link"})
        if record.get("actionAt"):
            return _response(409, {"error": "A response has already been recorded"})
        if method == "GET":
            # Email scanners and browser prefetches must never mutate feedback.
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "text/html", "Cache-Control": "no-store",
                            "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'"},
                "body": f'<html><body><h2>Confirm {escape(action)}</h2>'
                        f'<p>Delivery: {escape(delivery_id)}</p>'
                        f'<form method="post"><input type="hidden" name="token" value="{escape(token, quote=True)}">'
                        '<button type="submit">Confirm response</button></form></body></html>',
            }
        now = datetime.utcnow().isoformat() + "Z"
        update = "SET feedback = :feedback, actionAt = :ts"
        values = {":feedback": _map_action_to_feedback(action), ":ts": now,
                  ":hash": digest, ":now": int(time.time())}
        if action == "acknowledge":
            update += ", acknowledgedAt = :ts"
        elif action == "escalate":
            # Records the request; immediate escalation orchestration remains a deployment gate.
            update += ", escalationRequestedAt = :ts"
        table.update_item(
            Key={"deliveryId": delivery_id}, UpdateExpression=update,
            ConditionExpression="attribute_exists(deliveryId) AND actionTokenHash = :hash AND actionTokenExpiresAt > :now AND attribute_not_exists(actionAt)",
            ExpressionAttributeValues=values,
        )
        return _response(200, {"deliveryId": delivery_id, "action": action, "recordedAt": now})
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _response(409, {"error": "Response already recorded or link expired"})
        logger.error("action_callback_failed", error=str(exc))
        return _response(500, {"error": "Internal error"})
    except Exception as exc:
        logger.error("action_callback_failed", error=str(exc))
        return _response(500, {"error": "Internal error"})


def _map_action_to_feedback(action):
    return {"acknowledge": "useful", "escalate": "escalate", "suppress": "noise"}.get(action, action)


def _response(status_code, body):
    return {"statusCode": status_code, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"}, "body": json.dumps(body)}
