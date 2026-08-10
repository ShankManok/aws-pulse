"""Unit tests for the predictive signal generator."""
import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def env_vars():
    with patch.dict(os.environ, {
        "PREDICTORS_TABLE_NAME": "pulse-predictors-test",
        "SIGNAL_STREAM_NAME": "pulse-signals-test",
        "SIGNAL_TABLE_NAME": "pulse-events-test",
        "STAGE": "dev",
    }):
        yield


@pytest.fixture
def mock_dynamodb():
    with patch("src.intelligence.predictor.dynamodb") as mock_ddb:
        table = MagicMock()
        mock_ddb.Table.return_value = table
        yield table


@pytest.fixture
def mock_cloudwatch():
    with patch("src.intelligence.predictor.cloudwatch") as mock_cw:
        yield mock_cw


@pytest.fixture
def mock_kinesis():
    with patch("src.intelligence.predictor.kinesis") as mock_k:
        mock_k.put_record.return_value = {"SequenceNumber": "seq-1"}
        yield mock_k


class TestPredictor:

    def test_no_predictors_configured(self, mock_dynamodb, mock_cloudwatch, mock_kinesis):
        """Should return 0 predictions when no predictors are configured."""
        mock_dynamodb.scan.return_value = {"Items": []}

        from src.intelligence.predictor import handler
        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["predictions_generated"] == 0

    def test_generates_prediction_when_breach_projected(self, mock_dynamodb, mock_cloudwatch, mock_kinesis):
        """Should generate a predictive signal when metric is trending toward breach."""
        # Configure a predictor
        mock_dynamodb.scan.return_value = {"Items": [{
            "predictorId": "pred-1",
            "namespace": "AWS/EC2",
            "metricName": "CPUUtilization",
            "dimensions": [{"name": "InstanceId", "value": "i-abc123"}],
            "threshold": 90.0,
            "statistic": "Average",
            "comparison": "GreaterThanThreshold",
            "resourceArn": "arn:aws:ec2:ap-southeast-1:123:instance/i-abc123",
            "tags": {"Environment": "production"},
            "notifyPersonas": ["sre"],
            "escalationChain": ["persona-sre", "persona-cto"],
            "enabled": True,
        }]}

        # Generate trending-up data points over 24h (reaching 90 in ~36h)
        now = datetime.utcnow()
        datapoints = []
        for i in range(288):  # 5-min intervals over 24h
            ts = now - timedelta(hours=24) + timedelta(minutes=i * 5)
            # Linear increase from 50 to 75 over 24h (slope will project breach at ~90 in ~36h)
            value = 50 + (25 * i / 287)
            datapoints.append({"Timestamp": ts, "Average": value})

        mock_cloudwatch.get_metric_statistics.return_value = {"Datapoints": datapoints}

        from src.intelligence.predictor import handler
        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["predictions_generated"] == 1
        mock_kinesis.put_record.assert_called_once()

    def test_no_prediction_when_trend_away_from_threshold(self, mock_dynamodb, mock_cloudwatch, mock_kinesis):
        """Should NOT generate signal when metric is trending away from threshold."""
        mock_dynamodb.scan.return_value = {"Items": [{
            "predictorId": "pred-2",
            "namespace": "AWS/EC2",
            "metricName": "CPUUtilization",
            "dimensions": [{"name": "InstanceId", "value": "i-abc123"}],
            "threshold": 90.0,
            "statistic": "Average",
            "comparison": "GreaterThanThreshold",
            "resourceArn": "arn:aws:ec2:ap-southeast-1:123:instance/i-abc123",
            "enabled": True,
        }]}

        # Decreasing data points
        now = datetime.utcnow()
        datapoints = []
        for i in range(50):
            ts = now - timedelta(hours=24) + timedelta(minutes=i * 30)
            value = 80 - (i * 0.5)  # Decreasing from 80
            datapoints.append({"Timestamp": ts, "Average": value})

        mock_cloudwatch.get_metric_statistics.return_value = {"Datapoints": datapoints}

        from src.intelligence.predictor import handler
        result = handler({}, None)

        assert result["predictions_generated"] == 0
        mock_kinesis.put_record.assert_not_called()

    def test_no_prediction_when_breach_beyond_72h(self, mock_dynamodb, mock_cloudwatch, mock_kinesis):
        """Should NOT generate signal when breach is projected > 72h away."""
        mock_dynamodb.scan.return_value = {"Items": [{
            "predictorId": "pred-3",
            "namespace": "AWS/EC2",
            "metricName": "CPUUtilization",
            "dimensions": [{"name": "InstanceId", "value": "i-abc123"}],
            "threshold": 90.0,
            "statistic": "Average",
            "comparison": "GreaterThanThreshold",
            "resourceArn": "arn:aws:ec2:ap-southeast-1:123:instance/i-abc123",
            "enabled": True,
        }]}

        # Very slow increase (would take > 100h to reach 90)
        now = datetime.utcnow()
        datapoints = []
        for i in range(50):
            ts = now - timedelta(hours=24) + timedelta(minutes=i * 30)
            value = 50 + (i * 0.1)  # Very slow increase
            datapoints.append({"Timestamp": ts, "Average": value})

        mock_cloudwatch.get_metric_statistics.return_value = {"Datapoints": datapoints}

        from src.intelligence.predictor import handler
        result = handler({}, None)

        assert result["predictions_generated"] == 0

    def test_insufficient_data_points_skipped(self, mock_dynamodb, mock_cloudwatch, mock_kinesis):
        """Should skip predictor when fewer than 6 data points available."""
        mock_dynamodb.scan.return_value = {"Items": [{
            "predictorId": "pred-4",
            "namespace": "AWS/EC2",
            "metricName": "CPUUtilization",
            "dimensions": [],
            "threshold": 90.0,
            "statistic": "Average",
            "comparison": "GreaterThanThreshold",
            "enabled": True,
        }]}

        # Only 3 data points
        now = datetime.utcnow()
        datapoints = [
            {"Timestamp": now - timedelta(hours=2), "Average": 70},
            {"Timestamp": now - timedelta(hours=1), "Average": 75},
            {"Timestamp": now, "Average": 80},
        ]
        mock_cloudwatch.get_metric_statistics.return_value = {"Datapoints": datapoints}

        from src.intelligence.predictor import handler
        result = handler({}, None)

        assert result["predictions_generated"] == 0


class TestLinearExtrapolation:

    def test_extrapolation_increasing(self):
        """Linear extrapolation should detect increasing trend toward threshold."""
        from src.intelligence.predictor import _extrapolate_time_to_breach

        now = datetime.utcnow()
        # 10 points over 10h increasing from 50 to 80
        data_points = [(now - timedelta(hours=10-i), 50 + 3*i) for i in range(10)]

        hours = _extrapolate_time_to_breach(data_points, 90.0, "GreaterThanThreshold")

        # Should predict breach in a few hours
        assert hours is not None
        assert 0 < hours < 10

    def test_extrapolation_decreasing_no_breach(self):
        """Decreasing trend should return None for GreaterThan threshold."""
        from src.intelligence.predictor import _extrapolate_time_to_breach

        now = datetime.utcnow()
        data_points = [(now - timedelta(hours=10-i), 80 - 3*i) for i in range(10)]

        hours = _extrapolate_time_to_breach(data_points, 90.0, "GreaterThanThreshold")
        assert hours is None

    def test_severity_from_hours(self):
        """Hours-to-breach should map to correct severity levels."""
        from src.intelligence.predictor import _severity_from_hours

        assert _severity_from_hours(12).value == "high"
        assert _severity_from_hours(36).value == "medium"
        assert _severity_from_hours(60).value == "low"
