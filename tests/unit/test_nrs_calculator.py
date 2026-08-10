"""Unit tests for the NRS calculator."""
import os
import pytest
from unittest.mock import patch, MagicMock, call
from decimal import Decimal


@pytest.fixture(autouse=True)
def env_vars():
    with patch.dict(os.environ, {
        "SIGNAL_TABLE_NAME": "pulse-events-test",
        "DELIVERY_TABLE_NAME": "pulse-delivery-test",
        "ANALYTICS_TABLE_NAME": "pulse-analytics-test",
        "ORG_ID": "default",
        "STAGE": "dev",
    }):
        yield


@pytest.fixture
def mock_dynamodb():
    with patch("src.learning.nrs_calculator.dynamodb") as mock_ddb:
        table = MagicMock()
        mock_ddb.Table.return_value = table
        yield table


@pytest.fixture
def mock_cloudwatch():
    with patch("src.learning.nrs_calculator.cloudwatch") as mock_cw:
        yield mock_cw


class TestNrsCalculator:

    def test_nrs_formula_basic(self, mock_dynamodb, mock_cloudwatch):
        """NRS = (suppressed + deduplicated) / total × 100."""
        # Setup: 100 total, 10 suppressed, 20 correlated
        call_count = [0]
        def scan_side_effect(**kwargs):
            call_count[0] += 1
            filter_expr = kwargs.get("FilterExpression", "")
            if "#status" in str(kwargs.get("ExpressionAttributeNames", {})):
                status = kwargs["ExpressionAttributeValues"].get(":status", "")
                if status == "suppressed":
                    return {"Count": 10}
                elif status == "correlated":
                    return {"Count": 20}
            else:
                # Total count (no status filter)
                return {"Count": 100}
            return {"Count": 0}

        mock_dynamodb.scan.side_effect = scan_side_effect

        # No acknowledged deliveries
        mock_dynamodb.scan.return_value = {"Count": 100, "Items": []}

        # Reset to use different returns per table
        tables = {}
        def table_factory(name):
            if name not in tables:
                tables[name] = MagicMock()
            return tables[name]

        with patch("src.learning.nrs_calculator.dynamodb") as mock_ddb:
            signal_table = MagicMock()
            delivery_table = MagicMock()
            analytics_table = MagicMock()

            def get_table(name):
                if "events" in name:
                    return signal_table
                elif "delivery" in name:
                    return delivery_table
                else:
                    return analytics_table

            mock_ddb.Table.side_effect = get_table

            # Signal table scan results
            def signal_scan(**kwargs):
                if "ExpressionAttributeNames" in kwargs and "#status" in kwargs.get("ExpressionAttributeNames", {}):
                    status = kwargs["ExpressionAttributeValues"].get(":status", "")
                    if status == "suppressed":
                        return {"Count": 10}
                    elif status == "correlated":
                        return {"Count": 20}
                return {"Count": 100}

            signal_table.scan.side_effect = signal_scan

            # Delivery table: 2 acknowledged deliveries
            delivery_table.scan.return_value = {
                "Items": [
                    {"deliveredAt": "2026-08-09T10:00:00Z", "acknowledgedAt": "2026-08-09T10:05:00Z"},
                    {"deliveredAt": "2026-08-09T11:00:00Z", "acknowledgedAt": "2026-08-09T11:10:00Z"},
                ]
            }

            from src.learning.nrs_calculator import handler
            result = handler({}, None)

        assert result["statusCode"] == 200
        # NRS = (10 + 20) / 100 × 100 = 30.0
        assert result["nrs"] == 30.0
        assert result["total_ingested"] == 100
        # MTTA = average(300s, 600s) = 450s
        assert result["mtta_seconds"] == 450.0

    def test_nrs_zero_signals(self, mock_dynamodb, mock_cloudwatch):
        """NRS should be 0 when no signals ingested."""
        mock_dynamodb.scan.return_value = {"Count": 0, "Items": []}

        from src.learning.nrs_calculator import handler
        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["nrs"] == 0.0
        assert result["total_ingested"] == 0

    def test_publishes_cloudwatch_metrics(self, mock_dynamodb, mock_cloudwatch):
        """Should publish NRS and MTTA to CloudWatch."""
        mock_dynamodb.scan.return_value = {"Count": 50, "Items": []}

        from src.learning.nrs_calculator import handler
        handler({}, None)

        mock_cloudwatch.put_metric_data.assert_called_once()
        call_kwargs = mock_cloudwatch.put_metric_data.call_args.kwargs
        assert call_kwargs["Namespace"] == "Pulse/Analytics"

        metric_names = [m["MetricName"] for m in call_kwargs["MetricData"]]
        assert "NotificationReductionScore" in metric_names
        assert "TotalSignalsIngested" in metric_names

    def test_stores_daily_snapshot(self, mock_dynamodb, mock_cloudwatch):
        """Should store snapshot in analytics DDB table."""
        mock_dynamodb.scan.return_value = {"Count": 10, "Items": []}

        from src.learning.nrs_calculator import handler
        handler({}, None)

        # Third table call is analytics table for put_item
        mock_dynamodb.put_item.assert_called_once()
        item = mock_dynamodb.put_item.call_args.kwargs["Item"]
        assert "snapshotId" in item
        assert "nrs" in item
        assert item["orgId"] == "default"

    def test_mtta_with_no_acknowledged(self, mock_dynamodb, mock_cloudwatch):
        """MTTA should be 0 when no deliveries are acknowledged."""
        mock_dynamodb.scan.return_value = {"Count": 10, "Items": []}

        from src.learning.nrs_calculator import handler
        result = handler({}, None)

        assert result["mtta_seconds"] == 0
