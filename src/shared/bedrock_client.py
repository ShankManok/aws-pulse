"""Bedrock invocation wrapper for Pulse."""
import json
import boto3
import structlog

logger = structlog.get_logger()

_client = None


def get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime")
    return _client


def invoke_model(
    prompt: str,
    model_id: str = "anthropic.claude-sonnet-4-20250514",
    max_tokens: int = 1024,
    temperature: float = 0.3,
) -> str:
    """Invoke a Bedrock model and return the text response."""
    client = get_client()

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    })

    response = client.invoke_model(
        modelId=model_id,
        body=body,
        contentType="application/json",
        accept="application/json",
    )

    result = json.loads(response["body"].read())
    return result["content"][0]["text"]


def score_severity(signal_data: dict) -> dict:
    """Use Bedrock to assess signal severity with blast radius."""
    prompt = f"""Assess the severity of this operational signal on a scale of 0-100.
Consider: blast radius (how many resources/accounts affected), business impact,
urgency, and historical patterns.

Signal: {json.dumps(signal_data, indent=2)}

Respond with JSON only:
{{"score": <0-100>, "level": "<critical|high|medium|low|informational>", "reasoning": "<1 sentence>"}}"""

    response = invoke_model(prompt, model_id="amazon.nova-pro-v1:0", max_tokens=256)
    return json.loads(response)


def transform_for_persona(signal_data: dict, persona_config: dict) -> str:
    """Transform signal content for a specific persona."""
    prompt = f"""Rewrite this operational signal for a {persona_config['role_template']} persona.

Language level: {persona_config['language_level']}
Max length: 3 sentences for executive, 1 paragraph for technical summary, full detail for detailed_technical.

Signal:
Title: {signal_data['content']['title']}
Detail: {signal_data['content']['raw_detail']}
Severity: {signal_data['severity']['level']} (score: {signal_data['severity']['score']})
Affected: {signal_data['context'].get('resource_arns', [])}

Write the notification text only. No preamble."""

    return invoke_model(prompt, max_tokens=512)
