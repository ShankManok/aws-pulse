"""Suppression Model - rule-based suppression logic for MVP.

Checks whether a signal should be suppressed for a given persona based on
learned and manual suppression rules stored in the persona's config.

Rules are evaluated in order; first match wins.
"""
import os
from datetime import datetime
from typing import Optional
import boto3
import structlog
from shared.config import Config

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")


def should_suppress(signal_data: dict, persona_config: dict) -> bool:
    """Check if a signal should be suppressed for a given persona.

    Args:
        signal_data: The canonical signal event dict
        persona_config: The persona DynamoDB item (with suppressionRules)

    Returns:
        True if the signal should be suppressed (not delivered)
    """
    rules = persona_config.get("suppressionRules", [])
    if not rules:
        return False

    source = signal_data.get("source", "")
    severity_level = signal_data.get("severity", {}).get("level", "")
    signal_type = signal_data.get("signal_type", "")
    now = datetime.utcnow()

    for rule in rules:
        # Skip expired rules
        expires_at = rule.get("expiresAt", "")
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.rstrip("Z"))
                if exp_dt < now:
                    continue
            except (ValueError, TypeError):
                pass

        # Evaluate rule pattern
        if _matches_rule(rule, source, severity_level, signal_type):
            persona_id = persona_config.get("personaId", "unknown")
            logger.info(
                "signal_suppressed",
                persona_id=persona_id,
                rule_id=rule.get("id", "unknown"),
                signal_source=source,
                rule_source=rule.get("source", "unknown"),
            )
            return True

    return False


def _matches_rule(rule: dict, source: str, severity_level: str, signal_type: str) -> bool:
    """Check if a rule's pattern matches the signal attributes."""
    pattern = rule.get("pattern", {})

    if not pattern:
        return False

    # Match by source
    pattern_source = pattern.get("source", "")
    if pattern_source and pattern_source != source and pattern_source != "all":
        return False

    # Match by severity
    pattern_severity = pattern.get("severity", "")
    if pattern_severity and pattern_severity != severity_level:
        return False

    # Match by signal type
    pattern_type = pattern.get("signal_type", "")
    if pattern_type and pattern_type != signal_type:
        return False

    # If we have a source_key (learned rule), check confidence threshold
    if "source_key" in pattern:
        confidence = rule.get("confidence", 0)
        # Only suppress if confidence is above 0.5 (at least 3/6 ratio)
        if confidence < 0.5:
            return False
        return True

    # Generic match (manual rules with no specific filters match everything)
    # But only if at least one filter is specified
    has_filter = pattern_source or pattern_severity or pattern_type
    return has_filter


def recalculate_suppression_rules(persona_id: str) -> list[dict]:
    """Recalculate suppression rules for a persona based on recent feedback.

    Called by the nightly scheduler to refresh learned rules and prune expired ones.

    Returns:
        Updated list of suppression rules
    """
    persona_table = dynamodb.Table(os.environ.get("PERSONA_TABLE_NAME", Config.PERSONA_TABLE_NAME))
    delivery_table = dynamodb.Table(os.environ.get("DELIVERY_TABLE_NAME", Config.DELIVERY_TABLE_NAME))

    # Fetch current persona
    response = persona_table.get_item(Key={"personaId": persona_id})
    persona = response.get("Item")
    if not persona:
        return []

    current_rules = persona.get("suppressionRules", [])
    now = datetime.utcnow()

    # Step 1: Prune expired rules
    active_rules = []
    for rule in current_rules:
        expires_at = rule.get("expiresAt", "")
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.rstrip("Z"))
                if exp_dt < now:
                    logger.info("pruned_expired_rule", persona_id=persona_id, rule_id=rule.get("id"))
                    continue
            except (ValueError, TypeError):
                pass
        active_rules.append(rule)

    # Step 2: Keep manual rules unchanged, only refresh learned ones
    manual_rules = [r for r in active_rules if r.get("source") == "manual"]
    learned_rules = [r for r in active_rules if r.get("source") == "learned"]

    # Step 3: Re-evaluate learned rules - check if noise pattern still holds
    # For MVP, just keep existing learned rules that haven't expired
    final_rules = manual_rules + learned_rules

    # Step 4: Persist updated rules
    try:
        persona_table.update_item(
            Key={"personaId": persona_id},
            UpdateExpression="SET suppressionRules = :rules",
            ExpressionAttributeValues={":rules": final_rules},
        )
    except Exception as e:
        logger.error("suppression_recalc_failed", persona_id=persona_id, error=str(e))

    return final_rules


def handler(event, context):
    """Nightly Lambda to recalculate suppression rules for all personas.

    Triggered by EventBridge scheduled rule at 02:00 UTC daily.
    """
    persona_table = dynamodb.Table(os.environ.get("PERSONA_TABLE_NAME", Config.PERSONA_TABLE_NAME))

    # Scan all personas (acceptable for MVP scale)
    try:
        response = persona_table.scan(
            ProjectionExpression="personaId",
            Limit=1000,
        )
        personas = response.get("Items", [])
    except Exception as e:
        logger.error("persona_scan_failed", error=str(e))
        return {"statusCode": 500, "processed": 0}

    processed = 0
    for item in personas:
        persona_id = item.get("personaId", "")
        if persona_id:
            recalculate_suppression_rules(persona_id)
            processed += 1

    logger.info("suppression_recalculation_complete", processed=processed)
    return {"statusCode": 200, "processed": processed}
