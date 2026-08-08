"""Publish API Lambda handler - ingests signals into Pulse."""
import json
import os
import boto3
import structlog
from shared.models import SignalEvent

logger = structlog.get_logger()
kinesis = boto3.client("kinesis")
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    """Handle POST /v1/signals requests."""
    try:
        body = json.loads(event.get("body", "{}"))

        # Validate and create signal event
        signal = SignalEvent(
            source=body["source"],
            signal_type=body["signal_type"],
            severity=body["severity"],
            content=body["content"],
            context=body.get("context", {}),
            audience_hint=body.get("audience_hint", {}),
            correlation=body.get("correlation", {}),
        )

        # Write to Kinesis for processing pipeline
        kinesis.put_record(
            StreamName=os.environ["SIGNAL_STREAM_NAME"],
            Data=json.dumps(signal.to_dynamo()),
            PartitionKey=signal.context.account_id or signal.signal_id,
        )

        # Write to DynamoDB for persistence
        table = dynamodb.Table(os.environ["SIGNAL_TABLE_NAME"])
        table.put_item(Item=signal.to_dynamo())

        logger.info("signal_ingested", signal_id=signal.signal_id, source=signal.source)

        return {
            "statusCode": 201,
            "body": json.dumps({
                "signalId": signal.signal_id,
                "status": signal.status.value,
            }),
        }

    except KeyError as e:
        return {"statusCode": 400, "body": json.dumps({"error": f"Missing field: {e}"})}
    except Exception as e:
        logger.error("publish_failed", error=str(e))
        return {"statusCode": 500, "body": json.dumps({"error": "Internal error"})}
