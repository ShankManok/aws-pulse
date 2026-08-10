"""NRS Calculator - daily metrics computation for Notification Reduction Score.

Calculates per-org metrics:
- NRS = (signals_suppressed + signals_deduplicated) / total_signals_ingested × 100
- MTTA = average(acknowledgedAt - deliveredAt) for acknowledged deliveries

Publishes to CloudWatch custom namespace "Pulse/Analytics" and stores daily
snapshots in pulse-analytics-{stage} DynamoDB table.
"""
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
import boto3
import structlog
from shared.config import Config

logger = structlog.get_logger()
dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")


def handler(event, context):
    """Daily NRS calculation Lambda. Triggered by EventBridge schedule.

    Computes metrics for the previous 24-hour window.
    """
    stage = os.environ.get("STAGE", Config.STAGE)
    signal_table = dynamodb.Table(os.environ.get("SIGNAL_TABLE_NAME", Config.SIGNAL_TABLE_NAME))
    delivery_table = dynamodb.Table(os.environ.get("DELIVERY_TABLE_NAME", Config.DELIVERY_TABLE_NAME))
    analytics_table = dynamodb.Table(os.environ.get("ANALYTICS_TABLE_NAME", f"pulse-analytics-{stage}"))

    now = datetime.utcnow()
    window_end = now.isoformat() + "Z"
    window_start = (now - timedelta(hours=24)).isoformat() + "Z"

    org_id = os.environ.get("ORG_ID", "default")

    logger.info("nrs_calculation_start", window_start=window_start, window_end=window_end)

    # --- Compute signal counts ---
    total_ingested = _count_signals_by_status(signal_table, window_start, None)
    signals_suppressed = _count_signals_by_status(signal_table, window_start, "suppressed")
    signals_deduplicated = _count_signals_by_status(signal_table, window_start, "correlated")

    # --- Compute NRS ---
    nrs = 0.0
    if total_ingested > 0:
        nrs = ((signals_suppressed + signals_deduplicated) / total_ingested) * 100

    # --- Compute MTTA ---
    mtta_seconds = _compute_mtta(delivery_table, window_start)

    # --- Publish to CloudWatch ---
    _publish_metrics(org_id, stage, nrs, mtta_seconds, total_ingested, signals_suppressed)

    # --- Store daily snapshot ---
    snapshot = {
        "snapshotId": f"{org_id}#{now.strftime('%Y-%m-%d')}",
        "orgId": org_id,
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": window_end,
        "nrs": Decimal(str(round(nrs, 2))),
        "mttaSeconds": Decimal(str(round(mtta_seconds, 1))) if mtta_seconds else Decimal("0"),
        "totalIngested": total_ingested,
        "suppressed": signals_suppressed,
        "deduplicated": signals_deduplicated,
        "stage": stage,
    }

    try:
        analytics_table.put_item(Item=snapshot)
        logger.info("nrs_snapshot_stored", snapshot_id=snapshot["snapshotId"], nrs=nrs)
    except Exception as e:
        logger.error("snapshot_store_failed", error=str(e))

    logger.info(
        "nrs_calculation_complete",
        nrs=round(nrs, 2),
        mtta_seconds=round(mtta_seconds, 1) if mtta_seconds else None,
        total_ingested=total_ingested,
        suppressed=signals_suppressed,
        deduplicated=signals_deduplicated,
    )

    return {
        "statusCode": 200,
        "nrs": round(nrs, 2),
        "mtta_seconds": round(mtta_seconds, 1) if mtta_seconds else 0,
        "total_ingested": total_ingested,
    }


def _count_signals_by_status(signal_table, window_start: str, status: str = None) -> int:
    """Count signals in the time window, optionally filtered by status.

    For MVP, we scan with a filter. Production would use a GSI on status+ingestedAt.
    """
    try:
        filter_expr = "ingestedAt > :start"
        expr_values: dict = {":start": window_start}

        if status:
            filter_expr += " AND #status = :status"
            response = signal_table.scan(
                FilterExpression=filter_expr,
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={**expr_values, ":status": status},
                Select="COUNT",
                Limit=10000,
            )
        else:
            response = signal_table.scan(
                FilterExpression=filter_expr,
                ExpressionAttributeValues=expr_values,
                Select="COUNT",
                Limit=10000,
            )

        return response.get("Count", 0)

    except Exception as e:
        logger.warning("signal_count_failed", status=status, error=str(e))
        return 0


def _compute_mtta(delivery_table, window_start: str) -> float:
    """Compute Mean Time To Acknowledge for deliveries in the window.

    Returns average seconds between deliveredAt and acknowledgedAt.
    """
    try:
        response = delivery_table.scan(
            FilterExpression="deliveredAt > :start AND attribute_exists(acknowledgedAt)",
            ExpressionAttributeValues={":start": window_start},
            ProjectionExpression="deliveredAt, acknowledgedAt",
            Limit=5000,
        )

        items = response.get("Items", [])
        if not items:
            return 0.0

        total_seconds = 0.0
        count = 0

        for item in items:
            delivered = item.get("deliveredAt", "")
            acknowledged = item.get("acknowledgedAt", "")
            if delivered and acknowledged:
                try:
                    d_dt = datetime.fromisoformat(delivered.rstrip("Z"))
                    a_dt = datetime.fromisoformat(acknowledged.rstrip("Z"))
                    diff = (a_dt - d_dt).total_seconds()
                    if diff >= 0:
                        total_seconds += diff
                        count += 1
                except (ValueError, TypeError):
                    pass

        return total_seconds / count if count > 0 else 0.0

    except Exception as e:
        logger.warning("mtta_computation_failed", error=str(e))
        return 0.0


def _publish_metrics(
    org_id: str,
    stage: str,
    nrs: float,
    mtta_seconds: float,
    total_ingested: int,
    suppressed: int,
):
    """Publish metrics to CloudWatch Pulse/Analytics namespace."""
    try:
        metrics = [
            {
                "MetricName": "NotificationReductionScore",
                "Value": nrs,
                "Unit": "Percent",
            },
            {
                "MetricName": "TotalSignalsIngested",
                "Value": total_ingested,
                "Unit": "Count",
            },
            {
                "MetricName": "SignalsSuppressed",
                "Value": suppressed,
                "Unit": "Count",
            },
        ]

        if mtta_seconds > 0:
            metrics.append({
                "MetricName": "MTTA",
                "Value": mtta_seconds,
                "Unit": "Seconds",
            })

        dimensions = [
            {"Name": "OrgId", "Value": org_id},
            {"Name": "Stage", "Value": stage},
        ]

        cloudwatch.put_metric_data(
            Namespace="Pulse/Analytics",
            MetricData=[
                {**m, "Dimensions": dimensions, "Timestamp": datetime.utcnow()}
                for m in metrics
            ],
        )

        logger.info("metrics_published", metric_count=len(metrics))

    except Exception as e:
        logger.warning("metrics_publish_failed", error=str(e))
