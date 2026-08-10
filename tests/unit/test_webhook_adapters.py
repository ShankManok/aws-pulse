"""Unit tests for webhook adapters."""
import base64
import hashlib
import hmac
import json
import os
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def env_vars():
    with patch.dict(os.environ, {
        "SIGNAL_STREAM_NAME": "pulse-signals-test",
        "SIGNAL_TABLE_NAME": "pulse-events-test",
        "PAGERDUTY_WEBHOOK_SECRET": "test-secret",
        "DATADOG_WEBHOOK_API_KEY": "test-dd-key",
        "SERVICENOW_WEBHOOK_USER": "snow_user",
        "SERVICENOW_WEBHOOK_PASS": "snow_pass",
        "STAGE": "dev",
    }):
        yield


@pytest.fixture
def mock_kinesis():
    with patch("src.ingestion.webhook_adapters.pagerduty.kinesis") as mock_pd, \
         patch("src.ingestion.webhook_adapters.datadog.kinesis") as mock_dd, \
         patch("src.ingestion.webhook_adapters.servicenow.kinesis") as mock_sn:
        for m in (mock_pd, mock_dd, mock_sn):
            m.put_record.return_value = {"SequenceNumber": "seq-1"}
        yield {"pagerduty": mock_pd, "datadog": mock_dd, "servicenow": mock_sn}


@pytest.fixture
def mock_dynamodb():
    with patch("src.ingestion.webhook_adapters.pagerduty.dynamodb") as mock_pd, \
         patch("src.ingestion.webhook_adapters.datadog.dynamodb") as mock_dd, \
         patch("src.ingestion.webhook_adapters.servicenow.dynamodb") as mock_sn:
        for m in (mock_pd, mock_dd, mock_sn):
            table = MagicMock()
            m.Table.return_value = table
        yield {"pagerduty": mock_pd, "datadog": mock_dd, "servicenow": mock_sn}


class TestPagerDutyAdapter:

    def test_valid_webhook_creates_signal(self, mock_kinesis, mock_dynamodb):
        """Valid PagerDuty webhook should create a signal."""
        from src.ingestion.webhook_adapters.pagerduty import handler

        body = json.dumps({
            "event": {
                "event_type": "incident.triggered",
                "data": {
                    "id": "PD123",
                    "title": "High CPU on prod-web-01",
                    "urgency": "high",
                    "status": "triggered",
                    "service": {"summary": "Production Web"},
                    "html_url": "https://pd.example.com/incidents/PD123",
                },
            },
        })
        sig = "v1=" + hmac.new(b"test-secret", body.encode(), hashlib.sha256).hexdigest()

        event = {"headers": {"X-PagerDuty-Signature": sig}, "body": body}
        result = handler(event, None)

        assert result["statusCode"] == 201
        resp = json.loads(result["body"])
        assert "signalId" in resp
        mock_kinesis["pagerduty"].put_record.assert_called_once()

    def test_invalid_signature_rejected(self, mock_kinesis, mock_dynamodb):
        """Invalid signature should return 401."""
        from src.ingestion.webhook_adapters.pagerduty import handler

        event = {"headers": {"X-PagerDuty-Signature": "v1=bad"}, "body": "{}"}
        result = handler(event, None)

        assert result["statusCode"] == 401
        mock_kinesis["pagerduty"].put_record.assert_not_called()

    def test_urgency_mapping(self, mock_kinesis, mock_dynamodb):
        """Should map PagerDuty urgency to correct severity."""
        from src.ingestion.webhook_adapters.pagerduty import _normalize_incident

        high_signal = _normalize_incident({"urgency": "high", "title": "A", "service": {"summary": "S"}}, "incident.triggered")
        low_signal = _normalize_incident({"urgency": "low", "title": "A", "service": {"summary": "S"}}, "incident.triggered")

        assert high_signal.severity.level.value == "high"
        assert low_signal.severity.level.value == "low"


class TestDatadogAdapter:

    def test_valid_webhook_creates_signal(self, mock_kinesis, mock_dynamodb):
        """Valid Datadog webhook should create a signal."""
        from src.ingestion.webhook_adapters.datadog import handler

        body = json.dumps({
            "title": "CPU > 90% on i-abc123",
            "priority": "P2",
            "alert_type": "metric_alert",
            "tags": ["env:prod", "region:us-east-1"],
            "body": "CPU utilization exceeded threshold",
        })

        event = {"headers": {"DD-API-KEY": "test-dd-key"}, "body": body}
        result = handler(event, None)

        assert result["statusCode"] == 201
        mock_kinesis["datadog"].put_record.assert_called_once()

    def test_invalid_api_key_rejected(self, mock_kinesis, mock_dynamodb):
        """Invalid API key should return 401."""
        from src.ingestion.webhook_adapters.datadog import handler

        event = {"headers": {"DD-API-KEY": "wrong-key"}, "body": "{}"}
        result = handler(event, None)

        assert result["statusCode"] == 401

    def test_priority_mapping(self, mock_kinesis, mock_dynamodb):
        """Should map Datadog priority to correct severity."""
        from src.ingestion.webhook_adapters.datadog import _normalize_alert

        p1 = _normalize_alert({"priority": "P1", "tags": [], "title": "X"})
        p3 = _normalize_alert({"priority": "P3", "tags": [], "title": "X"})
        p5 = _normalize_alert({"priority": "P5", "tags": [], "title": "X"})

        assert p1.severity.level.value == "critical"
        assert p3.severity.level.value == "medium"
        assert p5.severity.level.value == "informational"

    def test_tags_parsed_to_context(self, mock_kinesis, mock_dynamodb):
        """Should parse Datadog tags into context."""
        from src.ingestion.webhook_adapters.datadog import _normalize_alert

        signal = _normalize_alert({
            "priority": "P3",
            "title": "Alert",
            "tags": ["region:us-east-1", "aws_account:123456789012"],
        })

        assert signal.context.region == "us-east-1"
        assert signal.context.account_id == "123456789012"


class TestServiceNowAdapter:

    def test_valid_webhook_creates_signal(self, mock_kinesis, mock_dynamodb):
        """Valid ServiceNow webhook should create a signal."""
        from src.ingestion.webhook_adapters.servicenow import handler

        body = json.dumps({
            "number": "INC001234",
            "short_description": "Database connection timeout",
            "impact": 1,
            "urgency": 1,
            "state": "New",
            "category": "Software",
        })
        creds = base64.b64encode(b"snow_user:snow_pass").decode()

        event = {"headers": {"Authorization": f"Basic {creds}"}, "body": body}
        result = handler(event, None)

        assert result["statusCode"] == 201
        mock_kinesis["servicenow"].put_record.assert_called_once()

    def test_invalid_basic_auth_rejected(self, mock_kinesis, mock_dynamodb):
        """Invalid Basic Auth should return 401."""
        from src.ingestion.webhook_adapters.servicenow import handler

        creds = base64.b64encode(b"wrong:creds").decode()
        event = {"headers": {"Authorization": f"Basic {creds}"}, "body": "{}"}
        result = handler(event, None)

        assert result["statusCode"] == 401

    def test_impact_urgency_matrix(self, mock_kinesis, mock_dynamodb):
        """Should map ServiceNow impact*urgency to correct severity."""
        from src.ingestion.webhook_adapters.servicenow import _normalize_incident

        critical = _normalize_incident({"impact": 1, "urgency": 1, "short_description": "X"})
        medium = _normalize_incident({"impact": 2, "urgency": 2, "short_description": "X"})
        low = _normalize_incident({"impact": 3, "urgency": 2, "short_description": "X"})

        assert critical.severity.level.value == "critical"
        assert medium.severity.level.value == "medium"
        assert low.severity.level.value == "low"

    def test_security_category_routes_to_ciso(self, mock_kinesis, mock_dynamodb):
        """Security category should set signal_type=finding and route to CISO."""
        from src.ingestion.webhook_adapters.servicenow import _normalize_incident

        signal = _normalize_incident({
            "impact": 2, "urgency": 1,
            "short_description": "Security breach",
            "category": "Security",
            "assignment_group": "Security Operations",
        })

        assert signal.signal_type.value == "finding"
        assert "ciso" in signal.audience_hint.personas
