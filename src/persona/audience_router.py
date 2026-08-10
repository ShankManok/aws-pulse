"""Audience Router - matches signals to personas based on audience hints and subscriptions."""
import json
import os
from typing import Optional
import boto3
import structlog
from shared.config import Config
from learning.suppression_model import should_suppress

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")

# MVP: hardcoded persona IDs for the 3 seeded personas
MVP_PERSONAS = ["persona-ciso", "persona-sre", "persona-cto"]

# Severity routing: which personas get notified at which severity levels
SEVERITY_ROUTING = {
    "critical": ["persona-ciso", "persona-sre", "persona-cto"],
    "high": ["persona-ciso", "persona-sre", "persona-cto"],
    "medium": ["persona-sre", "persona-cto"],
    "low": ["persona-sre"],
    "informational": ["persona-sre"],
}


def handler(event, context):
    """Route a signal to matching personas.

    Input (from Step Functions):
        {
            "signal": { ... signal data ... }
        }

    Output:
        {
            "signal": { ... signal data ... },
            "persona_ids": ["persona-ciso", "persona-sre"]
        }
    """
    signal_data = event.get("signal")

    if not signal_data:
        logger.error("missing_signal_data")
        return {"signal": signal_data, "persona_ids": []}

    signal_id = signal_data.get("signal_id", "unknown")
    matched_personas = set()

    # Strategy 1: Use audience_hint if provided
    audience_hint = signal_data.get("audience_hint", {})
    hinted_personas = audience_hint.get("personas", [])

    if hinted_personas:
        # Map role names to persona IDs
        for hint in hinted_personas:
            persona_id = _resolve_persona_hint(hint)
            if persona_id:
                matched_personas.add(persona_id)

    # Strategy 2: Route by severity level
    severity = signal_data.get("severity", {})
    severity_level = severity.get("level", "medium")
    severity_personas = SEVERITY_ROUTING.get(severity_level, ["persona-sre"])
    matched_personas.update(severity_personas)

    # Strategy 3: Check signal type routing
    signal_type = signal_data.get("signal_type", "")
    if signal_type == "finding":
        # Security findings always go to CISO
        matched_personas.add("persona-ciso")
    elif signal_type == "recommendation":
        # Recommendations go to CTO
        matched_personas.add("persona-cto")

    # Validate personas exist in DynamoDB and apply suppression rules
    persona_table = dynamodb.Table(os.environ.get("PERSONA_TABLE_NAME", Config.PERSONA_TABLE_NAME))
    valid_personas = _validate_and_filter_personas(persona_table, list(matched_personas), signal_data)

    logger.info(
        "audience_routed",
        signal_id=signal_id,
        severity=severity_level,
        matched_count=len(valid_personas),
        persona_ids=valid_personas,
    )

    return {"signal": signal_data, "persona_ids": valid_personas}


def _resolve_persona_hint(hint: str) -> Optional[str]:
    """Resolve a role hint string to a persona ID."""
    hint_lower = hint.lower()
    mapping = {
        "ciso": "persona-ciso",
        "security": "persona-ciso",
        "sre": "persona-sre",
        "devops": "persona-sre",
        "ops": "persona-sre",
        "cloud_ops": "persona-sre",
        "cto": "persona-cto",
        "engineering": "persona-cto",
        "vp_engineering": "persona-cto",
    }
    return mapping.get(hint_lower)


def _validate_and_filter_personas(table, persona_ids: list[str], signal_data: dict) -> list[str]:
    """Validate personas exist and filter out those with active suppression rules."""
    valid = []
    for persona_id in persona_ids:
        try:
            response = table.get_item(Key={"personaId": persona_id})
            persona_config = response.get("Item")

            if not persona_config:
                continue

            # Check suppression rules - skip delivery if suppressed
            if should_suppress(signal_data, persona_config):
                logger.info(
                    "persona_suppressed",
                    persona_id=persona_id,
                    signal_id=signal_data.get("signal_id", "unknown"),
                    source=signal_data.get("source", ""),
                )
                continue

            valid.append(persona_id)
        except Exception as e:
            logger.warning("persona_validation_failed", persona_id=persona_id, error=str(e))
    return valid
