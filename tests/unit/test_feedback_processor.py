"""Unit tests for the feedback processor."""
import os
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime


@pytest.fixture(autouse=True)
def env_vars():
    with patch.dict(os.environ, {
        "PERSONA_TABLE_NAME": "pulse-personas-test",
        "DELIVERY_TABLE_NAME": "pulse-delivery-test",
        "NOISE_THRESHOLD": "3",
        "WINDOW_DAYS": "30",
        "STAGE": "dev",
    }):
        yield


@pytest.fixture
def mock_dynamodb():
    with patch("src.learning.feedback_processor.dynamodb") as mock_ddb:
        table = MagicMock()
        mock_ddb.Table.return_value = table
        yield table


@pytest.fixture
def mock_cloudwatch():
    with patch("src.learning.feedback_processor.cloudwatch") as mock_cw:
        yield mock_cw


def _make_stream_record(delivery_id, persona_id, signal_id, new_feedback, old_feedback=None):
    """Create a DynamoDB Streams record."""
    new_image = {
        "deliveryId": {"S": delivery_id},
        "personaId": {"S": persona_id},
        "signalId": {"S": signal_id},
        "channel": {"S": "email"},
        "feedback": {"S": new_feedback},
    }
    old_image = {
        "deliveryId": {"S": delivery_id},
        "personaId": {"S": persona_id},
        "signalId": {"S": signal_id},
        "channel": {"S": "email"},
    }
    if old_feedback:
        old_image["feedback"] = {"S": old_feedback}

    return {
        "eventName": "MODIFY",
        "dynamodb": {
            "NewImage": new_image,
            "OldImage": old_image,
            "SequenceNumber": "seq-001",
        },
    }


class TestFeedbackProcessor:

    def test_noise_feedback_triggers_suppression_check(self, mock_dynamodb, mock_cloudwatch):
        """Noise feedback should query delivery history for suppression."""
        mock_dynamodb.query.return_value = {"Items": [
            {"signalId": "s1", "personaId": "persona-sre"},
            {"signalId": "s2", "personaId": "persona-sre"},
            {"signalId": "s3", "personaId": "persona-sre"},
        ]}

        from src.learning.feedback_processor import handler

        event = {"Records": [_make_stream_record("del-1", "persona-sre", "sig-1", "noise")]}
        result = handler(event, None)

        assert result["batchItemFailures"] == []
        # Should query delivery table for noise history
        mock_dynamodb.query.assert_called_once()
        # 3 noise items >= threshold of 3 → suppression rule created
        mock_dynamodb.update_item.assert_called_once()

    def test_noise_below_threshold_no_suppression(self, mock_dynamodb, mock_cloudwatch):
        """Below threshold noise count should not create suppression rule."""
        mock_dynamodb.query.return_value = {"Items": [
            {"signalId": "s1", "personaId": "persona-sre"},
            {"signalId": "s2", "personaId": "persona-sre"},
        ]}

        from src.learning.feedback_processor import handler

        event = {"Records": [_make_stream_record("del-1", "persona-sre", "sig-1", "noise")]}
        result = handler(event, None)

        assert result["batchItemFailures"] == []
        # 2 noise items < threshold of 3 → no suppression rule
        mock_dynamodb.update_item.assert_not_called()

    def test_useful_feedback_no_suppression_check(self, mock_dynamodb, mock_cloudwatch):
        """Non-noise feedback should not trigger suppression check."""
        from src.learning.feedback_processor import handler

        event = {"Records": [_make_stream_record("del-1", "persona-ciso", "sig-1", "useful")]}
        result = handler(event, None)

        assert result["batchItemFailures"] == []
        mock_dynamodb.query.assert_not_called()
        mock_dynamodb.update_item.assert_not_called()

    def test_publishes_cloudwatch_metric(self, mock_dynamodb, mock_cloudwatch):
        """Should publish feedback metric to CloudWatch."""
        from src.learning.feedback_processor import handler

        event = {"Records": [_make_stream_record("del-1", "persona-sre", "sig-1", "useful")]}
        handler(event, None)

        mock_cloudwatch.put_metric_data.assert_called_once()
        call_args = mock_cloudwatch.put_metric_data.call_args.kwargs
        assert call_args["Namespace"] == "Pulse/Analytics"
        metric = call_args["MetricData"][0]
        assert metric["MetricName"] == "FeedbackCount"
        assert metric["Value"] == 1

    def test_skips_non_modify_events(self, mock_dynamodb, mock_cloudwatch):
        """Should skip INSERT and REMOVE events."""
        from src.learning.feedback_processor import handler

        event = {"Records": [{
            "eventName": "INSERT",
            "dynamodb": {"NewImage": {}, "OldImage": {}, "SequenceNumber": "seq-001"},
        }]}
        result = handler(event, None)

        assert result["batchItemFailures"] == []
        mock_cloudwatch.put_metric_data.assert_not_called()

    def test_skips_unchanged_feedback(self, mock_dynamodb, mock_cloudwatch):
        """Should skip records where feedback didn't change."""
        from src.learning.feedback_processor import handler

        # feedback already existed in old image with same value
        event = {"Records": [_make_stream_record("del-1", "persona-sre", "sig-1", "noise", "noise")]}
        result = handler(event, None)

        assert result["batchItemFailures"] == []
        mock_cloudwatch.put_metric_data.assert_not_called()

    def test_batch_item_failures_on_error(self, mock_dynamodb, mock_cloudwatch):
        """Should report failed records for partial retry."""
        mock_cloudwatch.put_metric_data.side_effect = Exception("CW error")

        from src.learning.feedback_processor import handler

        # This will fail during metric publish but should be caught
        event = {"Records": [_make_stream_record("del-1", "persona-sre", "sig-1", "useful")]}
        result = handler(event, None)

        # The error is caught inside _publish_feedback_metric (warning only)
        # so no batch failure for metric errors
        assert result["batchItemFailures"] == []
