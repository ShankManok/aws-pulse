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
from shared.runtime import authorize, body as decode_body, owns
from shared.personas import SubscriptionFilter
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
        authorize(event)
        path_params = event.get("pathParameters", {}) or {}
        persona_id = path_params.get("personaId")
        body = decode_body(event)
        nl_text = body.get("naturalLanguage", "")

        if not persona_id:
            return _response(400, {"error": "Missing personaId in path"})

        if not isinstance(nl_text, str) or not 1 <= len(nl_text.strip()) <= 2000:
            return _response(400, {"error": "Missing naturalLanguage in request body"})

        persona_table = dynamodb.Table(os.environ.get("PERSONA_TABLE_NAME", Config.PERSONA_TABLE_NAME))
        persona = persona_table.get_item(Key={"personaId": persona_id}, ConsistentRead=True).get('Item')
        if not persona or not owns(persona):
            return _response(404, {'error': 'Persona not found'})
        if len(persona.get('subscriptions', [])) >= 100:
            return _response(409, {'error': 'Subscription limit reached'})

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
            ConditionExpression="attribute_exists(personaId) AND (attribute_not_exists(subscriptions) OR size(subscriptions) < :limit)",
            UpdateExpression="SET subscriptions = list_append(if_not_exists(subscriptions, :empty), :sub)",
            ExpressionAttributeValues={
                ":limit": 100,
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

    except PermissionError:
        return _response(403, {'error': 'Caller is not authorized for this organization'})
    except (ValueError, TypeError):
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

    except (ValueError, IndexError) as exc:
        raise ValueError('Could not produce a valid subscription; please rephrase') from exc


def _validate_filter(raw_filter: dict) -> dict:
    """Reject invalid constraints instead of silently broadening a subscription."""
    return SubscriptionFilter.model_validate(raw_filter).model_dump(exclude_none=True)


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
