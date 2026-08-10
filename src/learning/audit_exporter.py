"""Audit Exporter - hourly export of DeliveryRecords to S3 in JSON Lines format.

Exports delivery records from the last hour to S3 for compliance audit trail.
Records are partitioned by date/hour for efficient Athena querying.
"""
import json
import os
from datetime import datetime, timedelta
import boto3
import structlog
from shared.config import Config

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")


def handler(event, context):
    """Hourly audit export Lambda. Triggered by EventBridge schedule.

    Exports delivery records from the previous hour to S3 in JSON Lines format.
    Partitioned as: audit/year=YYYY/month=MM/day=DD/hour=HH/records.jsonl
    """
    stage = os.environ.get("STAGE", Config.STAGE)
    bucket_name = os.environ.get("AUDIT_BUCKET_NAME", "")
    delivery_table_name = os.environ.get("DELIVERY_TABLE_NAME", Config.DELIVERY_TABLE_NAME)

    if not bucket_name:
        logger.error("missing_audit_bucket_name")
        return {"statusCode": 500, "error": "AUDIT_BUCKET_NAME not set"}

    table = dynamodb.Table(delivery_table_name)

    # Time window: previous hour
    now = datetime.utcnow()
    window_end = now.replace(minute=0, second=0, microsecond=0)
    window_start = window_end - timedelta(hours=1)

    window_start_str = window_start.isoformat() + "Z"
    window_end_str = window_end.isoformat() + "Z"

    logger.info("audit_export_start", window_start=window_start_str, window_end=window_end_str)

    # Scan delivery records in the time window
    records = _scan_deliveries(table, window_start_str, window_end_str)

    if not records:
        logger.info("audit_export_empty", window_start=window_start_str)
        return {"statusCode": 200, "exported": 0}

    # Build JSON Lines content
    jsonl_content = "\n".join(json.dumps(record, default=str) for record in records)

    # S3 key with Hive-style partitioning for Athena
    s3_key = (
        f"audit/year={window_start.year:04d}/month={window_start.month:02d}/"
        f"day={window_start.day:02d}/hour={window_start.hour:02d}/records.jsonl"
    )

    try:
        s3.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=jsonl_content.encode("utf-8"),
            ContentType="application/jsonlines",
            ServerSideEncryption="AES256",
        )

        logger.info(
            "audit_export_complete",
            bucket=bucket_name,
            key=s3_key,
            record_count=len(records),
        )

        return {
            "statusCode": 200,
            "exported": len(records),
            "s3_key": s3_key,
        }

    except Exception as e:
        logger.error("audit_export_failed", error=str(e), bucket=bucket_name, key=s3_key)
        return {"statusCode": 500, "error": str(e)}


def _scan_deliveries(table, window_start: str, window_end: str) -> list[dict]:
    """Scan delivery records within the time window.

    Uses scan with filter (acceptable for hourly batches at MVP scale).
    Production would use a GSI on deliveredAt.
    """
    all_items = []

    try:
        paginator_kwargs = {
            "FilterExpression": "deliveredAt BETWEEN :start AND :end_time",
            "ExpressionAttributeValues": {
                ":start": window_start,
                ":end_time": window_end,
            },
        }

        response = table.scan(**paginator_kwargs)
        all_items.extend(response.get("Items", []))

        # Handle pagination
        while "LastEvaluatedKey" in response:
            response = table.scan(
                **paginator_kwargs,
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            all_items.extend(response.get("Items", []))

    except Exception as e:
        logger.error("delivery_scan_failed", error=str(e))

    return all_items
