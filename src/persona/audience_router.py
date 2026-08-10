"""Audience Router - matches signals to personas based on audience hints, subscriptions, and suppression rules."""
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

# Severity level ordering for subscription severity_min filter
SEVERITY_ORDER = {
    "critical": 5,
    "high": 4,
    "medium": 3,
    "low": 2,
    "informational": 1,
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
        matched_personas.add("persona-ciso")
    elif signal_type == "recommendation":
        matched_personas.add("persona-cto")

    # Validate personas, apply suppression rules, and check subscriptions
    persona_table = dynamodb.Table(os.environ.get("PERSONA_TABLE_NAME", Config.PERSONA_TABLE_NAME))
    valid_personas = _validate_and_filter_personas(persona_table, list(matched_personas), signal_data)

    # Strategy 4: Check all personas' subscriptions (may add personas not matched by default routing)
    subscription_matches = _check_subscriptions(persona_table, signal_data, set(valid_personas))
    if subscription_matches:
        valid_personas = list(set(valid_personas) | subscription_matches)

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


def _check_subscriptions(table, signal_data: dict, already_matched: set) -> set:
    """Check all personas' subscriptions to find additional matches.

    Scans personas and evaluates their subscriptions against the signal.
    Returns persona IDs that match via subscription but aren't already routed.
    """
    additional_matches = set()

    try:
        # Scan all personas (acceptable at MVP scale with ~10s of personas)
        response = table.scan(
            ProjectionExpression="personaId, subscriptions, suppressionRules",
            Limit=200,
        )
        personas = response.get("Items", [])
    except Exception as e:
        logger.warning("subscription_scan_failed", error=str(e))
        return additional_matches

    for persona in personas:
        persona_id = persona.get("personaId", "")
        if persona_id in already_matched:
            continue

        subscriptions = persona.get("subscriptions", [])
        if not subscriptions:
            continue

        # Check if signal matches any of this persona's subscriptions
        for sub in subscriptions:
            if not sub.get("enabled", True):
                continue

            sub_filter = sub.get("filter", {})
            if _signal_matches_subscription(signal_data, sub_filter):
                # Check suppression before adding
                if not should_suppress(signal_data, persona):
                    additional_matches.add(persona_id)
                    logger.info(
                        "subscription_match",
                        persona_id=persona_id,
                        subscription_id=sub.get("id", "unknown"),
                    )
                break  # One match is enough for this persona

    return additional_matches


def _signal_matches_subscription(signal_data: dict, sub_filter: dict) -> bool:
    """Evaluate whether a signal matches a subscription filter.

    All specified filter fields must match (AND logic).
    Within a list field, any value matching is sufficient (OR logic).
    """
    if not sub_filter:
        return False

    # Check sources
    if "sources" in sub_filter:
        signal_source = signal_data.get("source", "")
        if not any(signal_source.startswith(s) or s == signal_source for s in sub_filter["sources"]):
            return False

    # Check severity_min
    if "severity_min" in sub_filter:
        signal_severity = signal_data.get("severity", {}).get("level", "informational")
        min_level = sub_filter["severity_min"]
        if SEVERITY_ORDER.get(signal_severity, 0) < SEVERITY_ORDER.get(min_level, 0):
            return False

    # Check regions
    if "regions" in sub_filter:
        signal_region = signal_data.get("context", {}).get("region", "")
        if signal_region and signal_region not in sub_filter["regions"]:
            return False

    # Check tags
    if "tags" in sub_filter:
        signal_tags = signal_data.get("context", {}).get("tags", {})
        for key, value in sub_filter["tags"].items():
            if signal_tags.get(key) != value:
                return False

    # Check signal_types
    if "signal_types" in sub_filter:
        signal_type = signal_data.get("signal_type", "")
        if signal_type and signal_type not in sub_filter["signal_types"]:
            return False

    # Check keywords (any keyword in title or raw_detail)
    if "keywords" in sub_filter:
        title = signal_data.get("content", {}).get("title", "").lower()
        detail = signal_data.get("content", {}).get("raw_detail", "").lower()
        text = f"{title} {detail}"
        if not any(kw.lower() in text for kw in sub_filter["keywords"]):
            return False

    return True
