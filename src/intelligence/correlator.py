"""Correlation Engine - processes Kinesis stream records and routes signals to persona workflow."""
import hashlib
import json
import os
import base64
from datetime import datetime
import boto3
import structlog
from shared.config import Config
from shared.runtime import parse_time, owns, tenant

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
            if not owns(signal_data):
                raise PermissionError("Organization mismatch")

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
                    raise

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
    """Atomically group equal resource sets in deterministic event-time windows."""
    ingested = signal_data.get('ingested_at') or datetime.utcnow().isoformat() + 'Z'
    bucket = int(parse_time(ingested).timestamp()) // time_window_seconds
    key = f"{tenant()}:{bucket}:" + '|'.join(sorted(set(resource_arns)))
    group_id = 'cg-' + hashlib.sha256(key.encode()).hexdigest()[:32]
    correlation_table.update_item(Key={'groupId': group_id},
        UpdateExpression='SET rootSignalId = if_not_exists(rootSignalId, :root), #status = :active, orgId = :org, createdAt = if_not_exists(createdAt, :now), updatedAt = :now, #ttl = :ttl ADD signals :signals, resourceSet :resources',
        ExpressionAttributeNames={'#status': 'status', '#ttl': 'ttl'},
        ExpressionAttributeValues={':root': signal_id, ':active': 'active', ':org': tenant(), ':now': ingested,
                                   ':ttl': (bucket + 1) * time_window_seconds + 86400, ':signals': {signal_id}, ':resources': set(resource_arns)})
    return group_id


def _start_persona_workflow(workflow_arn: str, signal_data: dict, signal_id: str):
    """Start the persona Step Functions workflow for this signal."""
    try:
        execution_name = "sig-" + hashlib.sha256(signal_id.encode()).hexdigest()
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
