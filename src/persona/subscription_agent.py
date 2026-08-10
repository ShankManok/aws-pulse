"""Subscription Agent - NL subscription parser using Bedrock.

Accepts natural language subscription rules and converts them to structured
filter objects stored in the persona's subscriptions[] array.
"""
import json
import os
from datetime import datetime
import boto3
import structlog
import ulid
from shared.config import Config
from shared.bedrock_client import invoke_model

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    """Handle POST /v1/personas/{personaId}/subscribe.

    Input (API Gateway event):
        pathParameters: { personaId: "persona-sre" }
        body: { "naturalLanguage": "Notify me when any production RDS fails over in ap-southeast-1" }

    Output:
        { subscriptionId, naturalLanguage, filter }
    """
    try:
        path_params = event.get("pathParameters", {}) or {}
        persona_id = path_params.get("personaId")
        body = json.loads(event.get("body", "{}"))
        nl_text = body.get("naturalLanguage", "")

        if not persona_id:
            return _response(400, {"error": "Missing personaId in path"})

        if not nl_text:
            return _response(400, {"error": "Missing naturalLanguage in request body"})

        # Use Bedrock to parse NL into structured filter
        structured_filter = _parse_nl_to_filter(nl_text)

        # Create subscription object
        subscription_id = str(ulid.new())
        subscription = {
            "id": subscription_id,
            "naturalLanguage": nl_text,
            "filter": structured_filter,
            "createdAt": datetime.utcnow().isoformat() + "Z",
            "enabled": True,
        }

        # Store in persona's subscriptions array
        persona_table = dynamodb.Table(os.environ.get("PERSONA_TABLE_NAME", Config.PERSONA_TABLE_NAME))

        persona_table.update_item(
            Key={"personaId": persona_id},
            UpdateExpression="SET subscriptions = list_append(if_not_exists(subscriptions, :empty), :sub)",
            ExpressionAttributeValues={
                ":sub": [subscription],
                ":empty": [],
            },
        )

        logger.info(
            "subscription_created",
            persona_id=persona_id,
            subscription_id=subscription_id,
            filter_keys=list(structured_filter.keys()),
        )

        return _response(201, {
            "subscriptionId": subscription_id,
            "naturalLanguage": nl_text,
            "filter": structured_filter,
        })

    except json.JSONDecodeError:
        return _response(400, {"error": "Invalid JSON body"})
    except Exception as e:
        logger.error("subscription_creation_failed", error=str(e))
        return _response(500, {"error": "Internal error"})


def _parse_nl_to_filter(nl_text: str) -> dict:
    """Use Bedrock to parse natural language subscription into structured filter.

    Returns a filter dict with optional keys:
        sources: list of source patterns (e.g., ["aws.rds", "pagerduty"])
        severity_min: minimum severity level to match (e.g., "medium")
        regions: list of AWS regions (e.g., ["ap-southeast-1"])
        tags: dict of tag key/value pairs to match (e.g., {"Environment": "production"})
        signal_types: list of signal types (e.g., ["incident", "finding"])
        keywords: list of keywords to match in title/detail
    """
    prompt = f"""Parse the following natural language notification subscription rule into a structured JSON filter.

Rule: "{nl_text}"

Extract the following fields (omit any that are not mentioned or cannot be inferred):
- sources: list of signal source patterns (e.g., "aws.rds", "aws.cloudwatch", "pagerduty", "datadog")
- severity_min: minimum severity level ("critical", "high", "medium", "low", "informational")
- regions: list of AWS regions (e.g., "us-east-1", "ap-southeast-1")
- tags: object of resource tag key/value pairs to match (e.g., {{"Environment": "production"}})
- signal_types: list of signal types ("incident", "finding", "recommendation", "prediction", "lifecycle")
- keywords: list of keywords that should appear in the signal title or detail

Respond with ONLY valid JSON. No explanation or markdown. Example:
{{"sources": ["aws.rds"], "regions": ["ap-southeast-1"], "tags": {{"Environment": "production"}}, "signal_types": ["incident"]}}"""

    try:
        response_text = invoke_model(prompt, max_tokens=512, temperature=0.1)
        # Strip any markdown formatting
        response_text = response_text.strip()
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1]
            response_text = response_text.rsplit("```", 1)[0]

        parsed = json.loads(response_text)

        # Validate and clean the filter
        return _validate_filter(parsed)

    except (json.JSONDecodeError, Exception) as e:
        logger.warning("nl_parse_failed", error=str(e), nl_text=nl_text)
        # Fallback: create a keyword-based filter
        return {"keywords": nl_text.lower().split()[:5]}


def _validate_filter(raw_filter: dict) -> dict:
    """Validate and clean a parsed filter, removing invalid fields."""
    valid = {}

    if "sources" in raw_filter and isinstance(raw_filter["sources"], list):
        valid["sources"] = [s for s in raw_filter["sources"] if isinstance(s, str)]

    if "severity_min" in raw_filter:
        valid_levels = ("critical", "high", "medium", "low", "informational")
        if raw_filter["severity_min"] in valid_levels:
            valid["severity_min"] = raw_filter["severity_min"]

    if "regions" in raw_filter and isinstance(raw_filter["regions"], list):
        valid["regions"] = [r for r in raw_filter["regions"] if isinstance(r, str)]

    if "tags" in raw_filter and isinstance(raw_filter["tags"], dict):
        valid["tags"] = {k: v for k, v in raw_filter["tags"].items() if isinstance(k, str) and isinstance(v, str)}

    if "signal_types" in raw_filter and isinstance(raw_filter["signal_types"], list):
        valid_types = ("incident", "finding", "recommendation", "prediction", "lifecycle")
        valid["signal_types"] = [t for t in raw_filter["signal_types"] if t in valid_types]

    if "keywords" in raw_filter and isinstance(raw_filter["keywords"], list):
        valid["keywords"] = [k for k in raw_filter["keywords"] if isinstance(k, str)][:10]

    return valid


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
