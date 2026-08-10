"""Unit tests for the Slack sender Lambda."""
import json
import os
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def env_vars():
    """Set required environment variables."""
    with patch.dict(os.environ, {
        "DELIVERY_TABLE_NAME": "pulse-delivery-test",
        "CHATBOT_SNS_TOPIC_ARN": "arn:aws:sns:ap-southeast-1:123456789012:pulse-chatbot-dev",
        "CALLBACK_API_URL": "https://callback.example.com/dev",
        "STAGE": "dev",
    }):
        yield


@pytest.fixture
def mock_dynamodb():
    """Mock DynamoDB resource and table."""
    with patch("src.delivery.slack_sender.dynamodb") as mock_ddb:
        table = MagicMock()
        mock_ddb.Table.return_value = table
        yield table


@pytest.fixture
def mock_sns():
    """Mock SNS client."""
    with patch("src.delivery.slack_sender.sns_client") as mock:
        mock.publish.return_value = {"MessageId": "msg-123"}
        yield mock


@pytest.fixture
def sample_event():
    """Sample Step Functions Map state input."""
    return {
        "delivery": {
            "persona_id": "persona-sre",
            "transformed_content": "Instance i-abc123 failed health checks. CPU at 98% for 10 minutes. Reboot recommended.",
            "channel": "slack",
            "recipients": ["sre-oncall-channel"],
        },
        "signal": {
            "signal_id": "01HXY123ABC",
            "source": "aws.cloudwatch",
            "signal_type": "incident",
            "severity": {"level": "high", "score": 75},
            "content": {"title": "EC2 instance unhealthy", "raw_detail": "CPU spike"},
            "context": {"account_id": "123456789012", "region": "ap-southeast-1"},
        },
    }


class TestSlackSender:
    """Tests for slack_sender.handler."""

    def test_successful_slack_delivery(self, mock_dynamodb, mock_sns, sample_event):
        """Should publish to SNS topic and record delivery."""
        from src.delivery.slack_sender import handler

        result = handler(sample_event, None)

        assert result["statusCode"] == 200
        assert result["delivered"] is True
        assert len(result["delivery_ids"]) == 1
        assert "slack" in result["delivery_ids"][0]

        # Verify SNS publish was called
        mock_sns.publish.assert_called_once()
        call_kwargs = mock_sns.publish.call_args.kwargs
        assert call_kwargs["TopicArn"] == "arn:aws:sns:ap-southeast-1:123456789012:pulse-chatbot-dev"
        assert "[HIGH]" in call_kwargs["Subject"]

        # Verify message content is Chatbot-formatted JSON
        message = json.loads(call_kwargs["Message"])
        assert message["version"] == "1.0"
        assert message["source"] == "custom"
        assert "HIGH" in message["content"]["title"]
        assert len(message["content"]["nextSteps"]) == 3  # ack, escalate, suppress

    def test_delivery_record_written(self, mock_dynamodb, mock_sns, sample_event):
        """Should write delivery record to DynamoDB."""
        from src.delivery.slack_sender import handler

        handler(sample_event, None)

        mock_dynamodb.put_item.assert_called_once()
        item = mock_dynamodb.put_item.call_args.kwargs["Item"]
        assert item["channel"] == "slack"
        assert item["personaId"] == "persona-sre"
        assert item["signalId"] == "01HXY123ABC"
        assert item["recipientId"] == "sre-oncall-channel"

    def test_no_recipients_returns_not_delivered(self, mock_dynamodb, mock_sns):
        """Should return delivered=False when no recipients."""
        from src.delivery.slack_sender import handler

        event = {
            "delivery": {
                "persona_id": "persona-sre",
                "transformed_content": "...",
                "channel": "slack",
                "recipients": [],
            },
            "signal": {"signal_id": "test-1"},
        }

        result = handler(event, None)

        assert result["delivered"] is False
        mock_sns.publish.assert_not_called()

    def test_sns_publish_failure(self, mock_dynamodb, mock_sns, sample_event):
        """Should handle SNS publish errors gracefully."""
        mock_sns.publish.side_effect = Exception("SNS throttled")

        from src.delivery.slack_sender import handler

        result = handler(sample_event, None)

        assert result["delivered"] is False
        assert len(result["failed"]) == 1
        assert "SNS throttled" in result["failed"][0]["error"]

    def test_multiple_recipients(self, mock_dynamodb, mock_sns, sample_event):
        """Should send to multiple Slack channels."""
        sample_event["delivery"]["recipients"] = ["sre-channel", "incidents-channel"]

        from src.delivery.slack_sender import handler

        result = handler(sample_event, None)

        assert result["delivered"] is True
        assert len(result["delivery_ids"]) == 2
        assert mock_sns.publish.call_count == 2

    def test_message_attributes_include_severity(self, mock_dynamodb, mock_sns, sample_event):
        """Should include severity and persona as SNS message attributes."""
        from src.delivery.slack_sender import handler

        handler(sample_event, None)

        call_kwargs = mock_sns.publish.call_args.kwargs
        attrs = call_kwargs["MessageAttributes"]
        assert attrs["severity"]["StringValue"] == "high"
        assert attrs["persona"]["StringValue"] == "persona-sre"

    def test_callback_urls_in_message(self, mock_dynamodb, mock_sns, sample_event):
        """Should include callback action URLs in the Slack message."""
        from src.delivery.slack_sender import handler

        handler(sample_event, None)

        call_kwargs = mock_sns.publish.call_args.kwargs
        message = json.loads(call_kwargs["Message"])
        next_steps = message["content"]["nextSteps"]

        assert any("acknowledge" in step for step in next_steps)
        assert any("escalate" in step for step in next_steps)
        assert any("suppress" in step for step in next_steps)
        assert all("https://callback.example.com/dev" in step for step in next_steps)
