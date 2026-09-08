"""NRS Calculator - daily metrics computation for Notification Reduction Score.

Calculates per-org metrics:
- NRS = (signals_suppressed + signals_deduplicated) / total_signals_ingested × 100
- MTTA = average(acknowledgedAt - deliveredAt) for acknowledged deliveries

Publishes to CloudWatch custom namespace "Pulse/Analytics" and stores daily
snapshots in pulse-analytics-{stage} DynamoDB table.
"""
import os
from datetime import datetime, timedelta
from decimal import Decimal
import boto3
import structlog
from shared.config import Config
from shared.runtime import pages, parse_time

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

    now = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = now.isoformat() + "Z"
    window_start = (now - timedelta(hours=24)).isoformat() + "Z"

    org_id = os.environ.get("ORG_ID", "default")

    logger.info("nrs_calculation_start", window_start=window_start, window_end=window_end)

    # --- Compute signal counts ---
    total_ingested = _count_signals_by_status(signal_table, window_start, None, window_end)
    signals_suppressed = _count_signals_by_status(signal_table, window_start, "suppressed", window_end)
    signals_deduplicated = _count_signals_by_status(signal_table, window_start, "deduplicated", window_end)

    # --- Compute NRS ---
    nrs = 0.0
    if total_ingested > 0:
        nrs = ((signals_suppressed + signals_deduplicated) / total_ingested) * 100

    # --- Compute MTTA ---
    mtta_seconds = _compute_mtta(delivery_table, window_start, window_end)

    # --- Publish to CloudWatch ---
    _publish_metrics(org_id, stage, nrs, mtta_seconds, total_ingested, signals_suppressed)

    # --- Store daily snapshot ---
    snapshot = {
        "snapshotId": f"{org_id}#{(now - timedelta(days=1)).strftime('%Y-%m-%d')}",
        "orgId": org_id,
        "date": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
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
        raise

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


def _count_signals_by_status(signal_table, window_start: str, status: str = None, window_end: str = None) -> int:
    """Count actual signal decisions, excluding outbox receipts and other tenants."""
    kwargs = {'FilterExpression': 'ingestedAt >= :start AND ingestedAt < :end AND (attribute_not_exists(recordType) OR recordType = :signal) AND (org_id = :org OR (attribute_not_exists(org_id) AND :org = :default))',
              'ExpressionAttributeValues': {':start': window_start, ':end': window_end or datetime.utcnow().isoformat() + 'Z', ':signal': 'signal', ':org': os.environ.get('ORG_ID', 'default'), ':default': 'default'}, 'Select': 'COUNT'}
    if status:
        kwargs['FilterExpression'] += ' AND #status = :status'
        kwargs['ExpressionAttributeNames'] = {'#status': 'status'}
        kwargs['ExpressionAttributeValues'][':status'] = status
    return sum(page.get('Count', 0) for page in pages(signal_table, **kwargs))


def _compute_mtta(delivery_table, window_start: str, window_end: str = None) -> float:
    values = []
    for page in pages(delivery_table,
            FilterExpression='deliveredAt >= :start AND deliveredAt < :end AND attribute_exists(acknowledgedAt) AND (orgId = :org OR (attribute_not_exists(orgId) AND :org = :default))',
            ExpressionAttributeValues={':start': window_start, ':end': window_end or datetime.utcnow().isoformat() + 'Z', ':org': os.environ.get('ORG_ID', 'default'), ':default': 'default'},
            ProjectionExpression='deliveredAt, acknowledgedAt'):
        for item in page.get('Items', []):
            try:
                diff = (parse_time(item['acknowledgedAt']) - parse_time(item['deliveredAt'])).total_seconds()
                if diff >= 0:
                    values.append(diff)
            except (ValueError, TypeError, KeyError):
                continue
    return sum(values) / len(values) if values else 0.0


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
        raise
