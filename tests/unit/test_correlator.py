"""Unit tests for the correlation engine."""
import json
import base64
import os
import pytest
from unittest.mock import patch, MagicMock


def _make_kinesis_record(signal_data, seq_number="seq-001"):
    """Create a properly-formatted Kinesis record (base64-encoded data)."""
    return {
        "kinesis": {
            "data": base64.b64encode(json.dumps(signal_data).encode()).decode(),
            "sequenceNumber": seq_number,
        }
    }


@pytest.fixture(autouse=True)
def env_vars():
    with patch.dict(os.environ, {
        "CORRELATION_TABLE_NAME": "pulse-correlations-test",
        "SIGNAL_TABLE_NAME": "pulse-events-test",
        "PERSONA_WORKFLOW_ARN": "",
        "STAGE": "dev",
    }):
        yield


@pytest.fixture
def mock_dynamodb():
    with patch("src.intelligence.correlator.dynamodb") as mock_ddb:
        table = MagicMock()
        mock_ddb.Table.return_value = table
        table.get_item.return_value = {}
        table.put_item.return_value = {}
        table.update_item.return_value = {}
        yield table


@pytest.fixture
def mock_sfn():
    with patch("src.intelligence.correlator.sfn_client") as mock:
        yield mock


class TestCorrelator:
    """Tests for correlator.handler."""

    def test_no_resources_skips_correlation(self, mock_dynamodb, mock_sfn):
        """Signals without resource ARNs should skip correlation and not trigger workflow (no ARN set)."""
        from src.intelligence.correlator import handler

        signal = {
            "signal_id": "test-001",
            "source": "aws.cloudwatch",
            "context": {"resource_arns": []},
            "ingested_at": "2026-08-07T12:00:00Z",
            "correlation": {"time_window_seconds": 300},
        }

        event = {"Records": [_make_kinesis_record(signal)]}
        result = handler(event, None)

        assert result["batchItemFailures"] == []
        # No correlation group created since no resource ARNs
        mock_dynamodb.put_item.assert_not_called()

    def test_signal_with_resources_creates_correlation_group(self, mock_dynamodb, mock_sfn):
        """Signals with resource ARNs should create a correlation group."""
        from src.intelligence.correlator import handler

        signal = {
            "signal_id": "test-002",
            "source": "aws.cloudwatch",
            "severity": {"level": "high", "score": 75, "blast_radius": {"services": ["ec2"]}},
            "context": {
                "account_id": "123456789012",
                "resource_arns": ["arn:aws:ec2:us-east-1:123:instance/i-abc123"],
            },
            "ingested_at": "2026-08-07T12:00:00Z",
            "correlation": {"time_window_seconds": 300},
        }

        event = {"Records": [_make_kinesis_record(signal)]}
        result = handler(event, None)

        assert result["batchItemFailures"] == []
        # Should have attempted to create or join a correlation group
        mock_dynamodb.get_item.assert_called()

    def test_workflow_triggered_when_arn_set(self, mock_dynamodb, mock_sfn):
        """When PERSONA_WORKFLOW_ARN is set, should start the workflow."""
        with patch.dict(os.environ, {"PERSONA_WORKFLOW_ARN": "arn:aws:states:us-east-1:123:stateMachine:test"}):
            from src.intelligence.correlator import handler

            signal = {
                "signal_id": "test-003",
                "source": "aws.cloudwatch",
                "context": {"resource_arns": []},
                "ingested_at": "2026-08-07T12:00:00Z",
                "correlation": {"time_window_seconds": 300},
            }

            event = {"Records": [_make_kinesis_record(signal)]}
            handler(event, None)

            mock_sfn.start_execution.assert_called_once()

    def test_batch_item_failure_on_error(self, mock_dynamodb, mock_sfn):
        """Should report failed records for partial retry."""
        from src.intelligence.correlator import handler

        # Invalid base64 data will cause a decode error
        event = {
            "Records": [{
                "kinesis": {
                    "data": "not-valid-json-or-base64!!!",
                    "sequenceNumber": "seq-fail-001",
                }
            }]
        }

        result = handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "seq-fail-001"

    def test_multiple_records_processed(self, mock_dynamodb, mock_sfn):
        """Should process all records in a batch."""
        from src.intelligence.correlator import handler

        signals = [
            {"signal_id": f"test-batch-{i}", "source": "aws.cloudwatch", "context": {"resource_arns": []}, "ingested_at": "2026-08-07T12:00:00Z", "correlation": {"time_window_seconds": 300}}
            for i in range(3)
        ]

        event = {"Records": [_make_kinesis_record(s, f"seq-{i}") for i, s in enumerate(signals)]}
        result = handler(event, None)

        assert result["batchItemFailures"] == []
