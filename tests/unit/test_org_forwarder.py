"""Unit tests for the cross-account org forwarder."""
import json
import os
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def env_vars():
    with patch.dict(os.environ, {
        "SIGNAL_STREAM_NAME": "pulse-signals-test",
        "SIGNAL_TABLE_NAME": "pulse-events-test",
        "STAGE": "dev",
    }):
        yield


@pytest.fixture
def mock_kinesis():
    with patch("src.ingestion.org_forwarder.kinesis") as mock_k:
        mock_k.put_record.return_value = {"SequenceNumber": "seq-1"}
        yield mock_k


@pytest.fixture
def mock_dynamodb():
    with patch("src.ingestion.org_forwarder.dynamodb") as mock_ddb:
        table = MagicMock()
        mock_ddb.Table.return_value = table
        yield table


class TestOrgForwarder:

    def test_cloudwatch_alarm_event(self, mock_kinesis, mock_dynamodb):
        """Should normalize a CloudWatch alarm state change event."""
        from src.ingestion.org_forwarder import handler

        event = {
            "source": "aws.cloudwatch",
            "detail-type": "CloudWatch Alarm State Change",
            "account": "987654321098",
            "region": "us-east-1",
            "time": "2026-08-10T10:00:00Z",
            "detail": {
                "alarmName": "HighCPU-prod-web",
                "state": {"value": "ALARM", "reason": "CPU > 80%"},
                "alarmArn": "arn:aws:cloudwatch:us-east-1:987654321098:alarm:HighCPU-prod-web",
            },
        }

        result = handler(event, None)

        assert result["statusCode"] == 201
        assert result["processed"] is True
        mock_kinesis.put_record.assert_called_once()

        # Verify signal content
        call_data = json.loads(mock_kinesis.put_record.call_args.kwargs["Data"])
        assert call_data["source"] == "aws.cloudwatch"
        assert call_data["signal_type"] == "incident"
        assert call_data["context"]["account_id"] == "987654321098"
        assert call_data["context"]["region"] == "us-east-1"
        assert "arn:aws:cloudwatch" in call_data["context"]["resource_arns"][0]

    def test_security_hub_finding_event(self, mock_kinesis, mock_dynamodb):
        """Should normalize a Security Hub finding event."""
        from src.ingestion.org_forwarder import handler

        event = {
            "source": "aws.securityhub",
            "detail-type": "Security Hub Findings - Imported",
            "account": "987654321098",
            "region": "us-east-1",
            "time": "2026-08-10T10:00:00Z",
            "detail": {
                "findings": [{
                    "Title": "S3 Bucket Public Access",
                    "Description": "Bucket prod-data allows public read",
                    "Severity": {"Label": "HIGH", "Normalized": 80},
                    "Resources": [{"Id": "arn:aws:s3:::prod-data"}],
                }],
            },
        }

        result = handler(event, None)

        assert result["statusCode"] == 201
        call_data = json.loads(mock_kinesis.put_record.call_args.kwargs["Data"])
        assert call_data["signal_type"] == "finding"
        assert call_data["severity"]["level"] == "high"
        assert call_data["severity"]["score"] == 80
        assert "arn:aws:s3:::prod-data" in call_data["context"]["resource_arns"]

    def test_guardduty_finding_event(self, mock_kinesis, mock_dynamodb):
        """Should normalize a GuardDuty finding event."""
        from src.ingestion.org_forwarder import handler

        event = {
            "source": "aws.guardduty",
            "detail-type": "GuardDuty Finding",
            "account": "987654321098",
            "region": "eu-west-1",
            "time": "2026-08-10T10:00:00Z",
            "detail": {
                "title": "UnauthorizedAccess:EC2/MaliciousIPCaller",
                "description": "EC2 instance communicating with known malicious IP",
                "severity": 8.0,
                "resource": {
                    "instanceDetails": {"instanceId": "i-0abc123def"},
                },
            },
        }

        result = handler(event, None)

        assert result["statusCode"] == 201
        call_data = json.loads(mock_kinesis.put_record.call_args.kwargs["Data"])
        assert call_data["signal_type"] == "finding"
        assert call_data["severity"]["level"] == "high"
        assert call_data["severity"]["score"] == 80
        assert "ciso" in call_data["audience_hint"]["personas"]

    def test_health_event(self, mock_kinesis, mock_dynamodb):
        """Should normalize an AWS Health event."""
        from src.ingestion.org_forwarder import handler

        event = {
            "source": "aws.health",
            "detail-type": "AWS Health Event",
            "account": "987654321098",
            "region": "ap-southeast-1",
            "time": "2026-08-10T10:00:00Z",
            "detail": {
                "service": "EC2",
                "eventTypeCode": "AWS_EC2_SYSTEM_MAINTENANCE_EVENT",
                "eventDescription": [{"latestDescription": "Scheduled maintenance on instance"}],
                "affectedEntities": [{"entityValue": "arn:aws:ec2:ap-southeast-1:987654321098:instance/i-xyz"}],
            },
        }

        result = handler(event, None)

        assert result["statusCode"] == 201
        call_data = json.loads(mock_kinesis.put_record.call_args.kwargs["Data"])
        assert call_data["signal_type"] == "incident"
        assert "EC2" in call_data["content"]["title"]
        assert "arn:aws:ec2" in call_data["context"]["resource_arns"][0]

    def test_cross_account_tag_added(self, mock_kinesis, mock_dynamodb):
        """Should mark signals as cross-account in tags."""
        from src.ingestion.org_forwarder import handler

        event = {
            "source": "aws.cloudwatch",
            "detail-type": "CloudWatch Alarm State Change",
            "account": "111222333444",
            "region": "us-west-2",
            "time": "2026-08-10T10:00:00Z",
            "detail": {"alarmName": "Test", "state": {"value": "ALARM"}},
        }

        handler(event, None)

        call_data = json.loads(mock_kinesis.put_record.call_args.kwargs["Data"])
        assert call_data["context"]["tags"]["cross_account"] == "true"

    def test_partition_key_uses_account_id(self, mock_kinesis, mock_dynamodb):
        """Kinesis partition key should use the source account ID."""
        from src.ingestion.org_forwarder import handler

        event = {
            "source": "aws.cloudwatch",
            "detail-type": "CloudWatch Alarm State Change",
            "account": "555666777888",
            "region": "us-east-1",
            "time": "2026-08-10T10:00:00Z",
            "detail": {"alarmName": "Test", "state": {"value": "ALARM"}},
        }

        handler(event, None)

        call_kwargs = mock_kinesis.put_record.call_args.kwargs
        assert call_kwargs["PartitionKey"] == "555666777888"
