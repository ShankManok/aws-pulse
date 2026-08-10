"""Predictive Signal Generator - CloudWatch metric trend analysis with linear extrapolation.

Queries CloudWatch metrics for monitored resources, performs linear regression on
the last 24h of data points, and generates predictive SignalEvents when a metric
is projected to breach its threshold within 72 hours.
"""
import json
import os
from datetime import datetime, timedelta
from typing import Optional
import boto3
import structlog
from shared.config import Config
from shared.models import SignalEvent, SignalContent, Severity, SeverityLevel, SignalContext, AudienceHint

logger = structlog.get_logger()
cloudwatch = boto3.client("cloudwatch")
kinesis = boto3.client("kinesis")
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    """Scheduled Lambda (every 6 hours) that checks metric trends.

    Reads predictor configurations from DynamoDB, queries CloudWatch for each,
    performs linear extrapolation, and publishes predictive signals when breach
    is projected within 72 hours.
    """
    stage = os.environ.get("STAGE", Config.STAGE)
    predictors_table_name = os.environ.get("PREDICTORS_TABLE_NAME", f"pulse-predictors-{stage}")
    stream_name = os.environ.get("SIGNAL_STREAM_NAME", Config.SIGNAL_STREAM_NAME)
    signal_table_name = os.environ.get("SIGNAL_TABLE_NAME", Config.SIGNAL_TABLE_NAME)

    predictors_table = dynamodb.Table(predictors_table_name)
    signal_table = dynamodb.Table(signal_table_name)

    # Load predictor configurations
    predictors = _load_predictors(predictors_table)

    if not predictors:
        logger.info("no_predictors_configured")
        return {"statusCode": 200, "predictions_generated": 0}

    predictions_generated = 0

    for predictor in predictors:
        try:
            prediction = _evaluate_predictor(predictor)
            if prediction:
                _publish_prediction(prediction, stream_name, signal_table)
                predictions_generated += 1
        except Exception as e:
            logger.error(
                "predictor_evaluation_failed",
                predictor_id=predictor.get("predictorId", "unknown"),
                error=str(e),
            )

    logger.info("prediction_cycle_complete", predictions_generated=predictions_generated)
    return {"statusCode": 200, "predictions_generated": predictions_generated}


def _load_predictors(table) -> list[dict]:
    """Load all active predictor configurations from DynamoDB."""
    try:
        response = table.scan(
            FilterExpression="enabled = :enabled",
            ExpressionAttributeValues={":enabled": True},
            Limit=100,
        )
        return response.get("Items", [])
    except Exception as e:
        logger.error("predictors_load_failed", error=str(e))
        return []


def _evaluate_predictor(predictor: dict) -> Optional[SignalEvent]:
    """Evaluate a single predictor: query metrics, extrapolate, generate signal if breach projected."""
    namespace = predictor.get("namespace", "")
    metric_name = predictor.get("metricName", "")
    dimensions = predictor.get("dimensions", [])
    threshold = float(predictor.get("threshold", 0))
    resource_arn = predictor.get("resourceArn", "")
    statistic = predictor.get("statistic", "Average")
    comparison = predictor.get("comparison", "GreaterThanThreshold")

    if not namespace or not metric_name:
        return None

    # Query last 24h of data at 5-minute granularity
    now = datetime.utcnow()
    start_time = now - timedelta(hours=24)

    data_points = _get_metric_data(namespace, metric_name, dimensions, statistic, start_time, now)

    if len(data_points) < 6:
        # Need at least 6 data points (30 min) for meaningful extrapolation
        return None

    # Linear extrapolation
    hours_to_breach = _extrapolate_time_to_breach(data_points, threshold, comparison)

    if hours_to_breach is None or hours_to_breach > 72:
        return None

    # Generate predictive signal
    severity_level = _severity_from_hours(hours_to_breach)
    severity_score = _score_from_hours(hours_to_breach)

    breach_time = now + timedelta(hours=hours_to_breach)

    return SignalEvent(
        source="aws.pulse.predictor",
        signal_type="prediction",
        severity=Severity(
            level=severity_level,
            score=severity_score,
        ),
        content=SignalContent(
            title=f"Predicted: {metric_name} will breach {threshold} in ~{int(hours_to_breach)}h on {resource_arn or namespace}",
            raw_detail=(
                f"Linear extrapolation of {namespace}/{metric_name} over the last 24h projects "
                f"threshold breach ({comparison}: {threshold}) at approximately "
                f"{breach_time.strftime('%Y-%m-%d %H:%M UTC')}. "
                f"Current trend: {len(data_points)} data points analyzed."
            ),
            structured_data={
                "predictor_id": predictor.get("predictorId", ""),
                "namespace": namespace,
                "metric_name": metric_name,
                "threshold": threshold,
                "comparison": comparison,
                "hours_to_breach": round(hours_to_breach, 1),
                "projected_breach_time": breach_time.isoformat() + "Z",
                "data_points_analyzed": len(data_points),
                "current_value": data_points[-1][1] if data_points else None,
            },
        ),
        context=SignalContext(
            resource_arns=[resource_arn] if resource_arn else [],
            tags=predictor.get("tags", {}),
        ),
        audience_hint=AudienceHint(
            personas=predictor.get("notifyPersonas", ["sre"]),
            escalation_chain=predictor.get("escalationChain", ["persona-sre", "persona-cto"]),
            sla_acknowledge_minutes=60,
        ),
    )


def _get_metric_data(
    namespace: str,
    metric_name: str,
    dimensions: list[dict],
    statistic: str,
    start_time: datetime,
    end_time: datetime,
) -> list[tuple[datetime, float]]:
    """Query CloudWatch for metric data points."""
    try:
        cw_dimensions = [{"Name": d["name"], "Value": d["value"]} for d in dimensions]

        response = cloudwatch.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=cw_dimensions,
            StartTime=start_time,
            EndTime=end_time,
            Period=300,  # 5-minute granularity
            Statistics=[statistic],
        )

        datapoints = response.get("Datapoints", [])
        # Sort by timestamp and extract (time, value) pairs
        sorted_points = sorted(datapoints, key=lambda dp: dp["Timestamp"])
        return [(dp["Timestamp"], dp[statistic]) for dp in sorted_points]

    except Exception as e:
        logger.warning("metric_query_failed", namespace=namespace, metric=metric_name, error=str(e))
        return []


def _extrapolate_time_to_breach(
    data_points: list[tuple[datetime, float]],
    threshold: float,
    comparison: str,
) -> Optional[float]:
    """Perform linear extrapolation to estimate hours until threshold breach.

    Uses simple linear regression (least squares) on the data points.
    Returns None if no breach is projected within 72h or trend is moving away.
    """
    if len(data_points) < 2:
        return None

    # Convert timestamps to hours from first point
    t0 = data_points[0][0]
    x_values = [(dp[0] - t0).total_seconds() / 3600.0 for dp in data_points]
    y_values = [dp[1] for dp in data_points]

    # Simple linear regression: y = mx + b
    n = len(x_values)
    sum_x = sum(x_values)
    sum_y = sum(y_values)
    sum_xy = sum(x * y for x, y in zip(x_values, y_values))
    sum_x2 = sum(x * x for x in x_values)

    denominator = n * sum_x2 - sum_x * sum_x
    if denominator == 0:
        return None

    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n

    # Check if trend is heading toward breach
    if comparison == "GreaterThanThreshold":
        if slope <= 0:
            return None  # Decreasing or flat, won't breach upward
        # Time to reach threshold: threshold = slope * t + intercept
        hours_from_start = (threshold - intercept) / slope
    elif comparison == "LessThanThreshold":
        if slope >= 0:
            return None  # Increasing or flat, won't breach downward
        hours_from_start = (threshold - intercept) / slope
    else:
        return None

    # Convert to hours from now
    current_hours = x_values[-1]
    hours_to_breach = hours_from_start - current_hours

    if hours_to_breach <= 0:
        # Already breached
        return 0.1  # Signal immediate

    return hours_to_breach


def _severity_from_hours(hours: float) -> SeverityLevel:
    """Map hours-to-breach to severity level."""
    if hours < 24:
        return SeverityLevel.HIGH
    elif hours < 48:
        return SeverityLevel.MEDIUM
    else:
        return SeverityLevel.LOW


def _score_from_hours(hours: float) -> int:
    """Map hours-to-breach to severity score."""
    if hours < 12:
        return 85
    elif hours < 24:
        return 70
    elif hours < 48:
        return 50
    else:
        return 30


def _publish_prediction(signal: SignalEvent, stream_name: str, signal_table):
    """Publish predictive signal to Kinesis and DynamoDB."""
    signal_dict = signal.to_dynamo()

    if stream_name:
        kinesis.put_record(
            StreamName=stream_name,
            Data=json.dumps(signal_dict),
            PartitionKey=signal.context.account_id or signal.signal_id,
        )

    try:
        signal_table.put_item(Item=signal_dict)
    except Exception as e:
        logger.warning("prediction_store_failed", signal_id=signal.signal_id, error=str(e))

    logger.info(
        "prediction_published",
        signal_id=signal.signal_id,
        metric=signal.content.structured_data.get("metric_name"),
        hours_to_breach=signal.content.structured_data.get("hours_to_breach"),
    )
