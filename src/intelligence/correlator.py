"""Correlation Engine - processes Kinesis stream records and routes signals to persona workflow."""
import json
import os
import base64
from datetime import datetime, timedelta
import boto3
import structlog
from shared.config import Config

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")
sfn_client = boto3.client("stepfunctions")


def handler(event, context):
    """Process Kinesis records: correlate signals and trigger persona workflow.

    For Phase 1 MVP: minimal correlation (group by resource ARN within time window),
    then immediately trigger the persona Step Functions workflow for each signal.

    Supports batch item failure reporting - returns failed record sequence numbers
    so only failed items are retried.
    """
    correlation_table = dynamodb.Table(os.environ.get("CORRELATION_TABLE_NAME", Config.CORRELATION_TABLE_NAME))
    signal_table = dynamodb.Table(os.environ.get("SIGNAL_TABLE_NAME", Config.SIGNAL_TABLE_NAME))
    workflow_arn = os.environ.get("PERSONA_WORKFLOW_ARN", "")

    batch_item_failures = []

    for record in event.get("Records", []):
        try:
            # Decode Kinesis record
            payload_raw = base64.b64decode(record["kinesis"]["data"])
            signal_data = json.loads(payload_raw)
            signal_id = signal_data.get("signal_id", "unknown")

            logger.info("processing_signal", signal_id=signal_id, source=signal_data.get("source"))

            # --- Correlation Logic (MVP: lightweight grouping) ---
            resource_arns = signal_data.get("context", {}).get("resource_arns", [])
            correlation_config = signal_data.get("correlation", {})
            time_window_seconds = correlation_config.get("time_window_seconds", 300)

            correlation_group_id = None

            if resource_arns:
                correlation_group_id = _find_or_create_correlation_group(
                    correlation_table=correlation_table,
                    signal_id=signal_id,
                    signal_data=signal_data,
                    resource_arns=resource_arns,
                    time_window_seconds=time_window_seconds,
                )

            # Update signal status to 'correlated' in signal table
            if correlation_group_id:
                signal_data["correlation_group_id"] = correlation_group_id
                signal_data["status"] = "correlated"
                try:
                    signal_table.update_item(
                        Key={
                            "signalId": signal_id,
                            "ingestedAt": signal_data.get("ingested_at", ""),
                        },
                        UpdateExpression="SET #status = :status, correlationGroupId = :gid",
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues={
                            ":status": "correlated",
                            ":gid": correlation_group_id,
                        },
                    )
                except Exception as e:
                    logger.warning("signal_update_failed", signal_id=signal_id, error=str(e))

            # --- Trigger Persona Workflow ---
            if workflow_arn:
                _start_persona_workflow(workflow_arn, signal_data, signal_id)

        except Exception as e:
            logger.error(
                "record_processing_failed",
                error=str(e),
                sequence_number=record["kinesis"]["sequenceNumber"],
            )
            batch_item_failures.append({
                "itemIdentifier": record["kinesis"]["sequenceNumber"]
            })

    return {"batchItemFailures": batch_item_failures}


def _find_or_create_correlation_group(
    correlation_table,
    signal_id: str,
    signal_data: dict,
    resource_arns: list[str],
    time_window_seconds: int,
) -> str:
    """Find an existing active correlation group for the same resources, or create one.

    MVP approach: use first resource ARN as a simple hash key for grouping.
    Full implementation would do overlap detection across all ARNs.
    """
    import hashlib

    # Create a deterministic group key from sorted resource ARNs
    arns_key = "|".join(sorted(resource_arns))
    group_hash = hashlib.sha256(arns_key.encode()).hexdigest()[:16]

    now = datetime.utcnow()
    window_start = now - timedelta(seconds=time_window_seconds)

    # Try to find an active group with this hash
    group_id = f"cg-{group_hash}"

    try:
        response = correlation_table.get_item(Key={"groupId": group_id})
        existing = response.get("Item")

        if existing and existing.get("status") == "active":
            # Check if within time window
            group_created = existing.get("createdAt", "")
            if group_created:
                created_dt = datetime.fromisoformat(group_created.rstrip("Z"))
                if created_dt >= window_start:
                    # Add signal to existing group
                    signals = existing.get("signals", [])
                    if signal_id not in signals:
                        signals.append(signal_id)
                        correlation_table.update_item(
                            Key={"groupId": group_id},
                            UpdateExpression="SET signals = :signals, updatedAt = :now",
                            ExpressionAttributeValues={
                                ":signals": signals,
                                ":now": now.isoformat() + "Z",
                            },
                        )
                    logger.info("signal_correlated", signal_id=signal_id, group_id=group_id)
                    return group_id

    except Exception as e:
        logger.warning("correlation_lookup_failed", error=str(e))

    # Create new correlation group
    severity = signal_data.get("severity", {})
    context = signal_data.get("context", {})

    correlation_table.put_item(Item={
        "groupId": group_id,
        "signals": [signal_id],
        "rootSignalId": signal_id,
        "timeWindow": {
            "start": window_start.isoformat() + "Z",
            "end": (now + timedelta(seconds=time_window_seconds)).isoformat() + "Z",
        },
        "servicesAffected": severity.get("blast_radius", {}).get("services", []),
        "accountsAffected": [context.get("account_id", "")] if context.get("account_id") else [],
        "unifiedSeverity": severity,
        "status": "active",
        "createdAt": now.isoformat() + "Z",
        "updatedAt": now.isoformat() + "Z",
        # TTL: expire after 24 hours
        "ttl": int((now + timedelta(hours=24)).timestamp()),
    })

    logger.info("correlation_group_created", group_id=group_id, signal_id=signal_id)
    return group_id


def _start_persona_workflow(workflow_arn: str, signal_data: dict, signal_id: str):
    """Start the persona Step Functions workflow for this signal."""
    try:
        execution_name = f"{signal_id}-{int(datetime.utcnow().timestamp())}"
        # Step Functions execution names: max 80 chars, alphanumeric + hyphens + underscores
        execution_name = execution_name[:80].replace(".", "-")

        sfn_client.start_execution(
            stateMachineArn=workflow_arn,
            name=execution_name,
            input=json.dumps({"signal": signal_data}),
        )

        logger.info("persona_workflow_started", signal_id=signal_id, execution=execution_name)

    except sfn_client.exceptions.ExecutionAlreadyExists:
        logger.warning("workflow_execution_exists", signal_id=signal_id)
    except Exception as e:
        logger.error("workflow_start_failed", signal_id=signal_id, error=str(e))
        raise  # Re-raise to trigger batch item failure
