"""Unit tests for the correlation engine."""
import json
import pytest
from unittest.mock import patch, MagicMock


def test_no_resources_skips_correlation():
    """Signals without resource ARNs should skip correlation."""
    from src.intelligence.correlator import handler

    event = {
        "Records": [{
            "kinesis": {
                "data": json.dumps({
                    "signal_id": "test-001",
                    "context": {"resource_arns": []},
                    "ingested_at": "2026-08-07T12:00:00Z",
                    "correlation": {"time_window_seconds": 300},
                }).encode()
            }
        }]
    }

    # Should not raise
    with patch.dict("os.environ", {"CORRELATION_TABLE_NAME": "test-table"}):
        with patch("boto3.resource"):
            result = handler(event, None)
            assert result["statusCode"] == 200
