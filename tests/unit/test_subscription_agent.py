"""Unit tests for the subscription agent."""
import json
import os
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def env_vars():
    with patch.dict(os.environ, {
        "PERSONA_TABLE_NAME": "pulse-personas-test",
        "STAGE": "dev",
    }):
        yield


@pytest.fixture
def mock_dynamodb():
    with patch("src.persona.subscription_agent.dynamodb") as mock_ddb:
        table = MagicMock()
        mock_ddb.Table.return_value = table
        yield table


@pytest.fixture
def mock_bedrock():
    with patch("src.persona.subscription_agent.invoke_model") as mock_br:
        yield mock_br


class TestSubscriptionAgent:

    def test_creates_subscription_from_nl(self, mock_dynamodb, mock_bedrock):
        """Should parse NL rule via Bedrock and store subscription."""
        mock_bedrock.return_value = json.dumps({
            "sources": ["aws.rds"],
            "regions": ["ap-southeast-1"],
            "tags": {"Environment": "production"},
            "signal_types": ["incident"],
        })

        from src.persona.subscription_agent import handler

        event = {
            "pathParameters": {"personaId": "persona-sre"},
            "body": json.dumps({
                "naturalLanguage": "Notify me when any production RDS fails over in ap-southeast-1",
            }),
        }

        result = handler(event, None)

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert "subscriptionId" in body
        assert body["filter"]["sources"] == ["aws.rds"]
        assert body["filter"]["regions"] == ["ap-southeast-1"]
        assert body["filter"]["tags"] == {"Environment": "production"}

        # Verify DynamoDB update was called
        mock_dynamodb.update_item.assert_called_once()

    def test_missing_persona_id_returns_400(self, mock_dynamodb, mock_bedrock):
        """Should return 400 when personaId is missing."""
        from src.persona.subscription_agent import handler

        event = {"pathParameters": {}, "body": json.dumps({"naturalLanguage": "test"})}
        result = handler(event, None)

        assert result["statusCode"] == 400

    def test_missing_natural_language_returns_400(self, mock_dynamodb, mock_bedrock):
        """Should return 400 when naturalLanguage is missing."""
        from src.persona.subscription_agent import handler

        event = {"pathParameters": {"personaId": "persona-sre"}, "body": json.dumps({})}
        result = handler(event, None)

        assert result["statusCode"] == 400

    def test_invalid_json_body_returns_400(self, mock_dynamodb, mock_bedrock):
        """Should return 400 for invalid JSON body."""
        from src.persona.subscription_agent import handler

        event = {"pathParameters": {"personaId": "persona-sre"}, "body": "not-json"}
        result = handler(event, None)

        assert result["statusCode"] == 400

    def test_bedrock_parse_failure_falls_back_to_keywords(self, mock_dynamodb, mock_bedrock):
        """Should fallback to keyword filter when Bedrock parse fails."""
        mock_bedrock.return_value = "not valid json at all"

        from src.persona.subscription_agent import handler

        event = {
            "pathParameters": {"personaId": "persona-sre"},
            "body": json.dumps({"naturalLanguage": "alert me on rds failures"}),
        }

        result = handler(event, None)

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        # Should have keywords fallback
        assert "keywords" in body["filter"]


class TestFilterValidation:

    def test_validates_severity_levels(self):
        """Should only accept valid severity levels."""
        from src.persona.subscription_agent import _validate_filter

        valid = _validate_filter({"severity_min": "high"})
        assert valid["severity_min"] == "high"

        invalid = _validate_filter({"severity_min": "super_critical"})
        assert "severity_min" not in invalid

    def test_validates_signal_types(self):
        """Should only accept valid signal types."""
        from src.persona.subscription_agent import _validate_filter

        valid = _validate_filter({"signal_types": ["incident", "finding", "invalid_type"]})
        assert valid["signal_types"] == ["incident", "finding"]

    def test_limits_keywords(self):
        """Should limit keywords to 10 entries."""
        from src.persona.subscription_agent import _validate_filter

        many_keywords = [f"kw{i}" for i in range(20)]
        valid = _validate_filter({"keywords": many_keywords})
        assert len(valid["keywords"]) == 10

    def test_empty_filter_removed(self):
        """Should remove empty lists and invalid types."""
        from src.persona.subscription_agent import _validate_filter

        result = _validate_filter({"sources": [], "tags": "not-a-dict"})
        # Empty list stays (it's valid, just empty)
        assert result.get("sources") == []
        # Invalid type tag is excluded
        assert "tags" not in result


class TestSubscriptionMatching:
    """Tests for the _signal_matches_subscription function in audience_router."""

    def test_source_filter_matches(self):
        """Should match when signal source matches subscription sources."""
        from src.persona.audience_router import _signal_matches_subscription

        signal = {"source": "aws.rds", "severity": {"level": "high"}, "signal_type": "incident", "content": {"title": "x", "raw_detail": ""}, "context": {}}
        sub_filter = {"sources": ["aws.rds"]}

        assert _signal_matches_subscription(signal, sub_filter) is True

    def test_source_filter_prefix_match(self):
        """Should match when signal source starts with a subscription source prefix."""
        from src.persona.audience_router import _signal_matches_subscription

        signal = {"source": "aws.rds.failover", "severity": {"level": "high"}, "signal_type": "incident", "content": {"title": "x", "raw_detail": ""}, "context": {}}
        sub_filter = {"sources": ["aws.rds"]}

        assert _signal_matches_subscription(signal, sub_filter) is True

    def test_severity_min_filter(self):
        """Should only match signals at or above the minimum severity."""
        from src.persona.audience_router import _signal_matches_subscription

        high_signal = {"source": "aws.ec2", "severity": {"level": "high"}, "signal_type": "incident", "content": {"title": "x", "raw_detail": ""}, "context": {}}
        low_signal = {"source": "aws.ec2", "severity": {"level": "low"}, "signal_type": "incident", "content": {"title": "x", "raw_detail": ""}, "context": {}}
        sub_filter = {"severity_min": "medium"}

        assert _signal_matches_subscription(high_signal, sub_filter) is True
        assert _signal_matches_subscription(low_signal, sub_filter) is False

    def test_region_filter(self):
        """Should only match signals from specified regions."""
        from src.persona.audience_router import _signal_matches_subscription

        signal = {"source": "aws.ec2", "severity": {"level": "high"}, "signal_type": "incident", "content": {"title": "x", "raw_detail": ""}, "context": {"region": "us-east-1"}}
        sub_filter = {"regions": ["ap-southeast-1", "us-east-1"]}

        assert _signal_matches_subscription(signal, sub_filter) is True

        signal2 = {"source": "aws.ec2", "severity": {"level": "high"}, "signal_type": "incident", "content": {"title": "x", "raw_detail": ""}, "context": {"region": "eu-west-1"}}
        assert _signal_matches_subscription(signal2, sub_filter) is False

    def test_keyword_filter(self):
        """Should match when any keyword appears in title or detail."""
        from src.persona.audience_router import _signal_matches_subscription

        signal = {"source": "aws.rds", "severity": {"level": "high"}, "signal_type": "incident", "content": {"title": "RDS failover detected", "raw_detail": "Cluster switched to replica"}, "context": {}}
        sub_filter = {"keywords": ["failover", "outage"]}

        assert _signal_matches_subscription(signal, sub_filter) is True

        signal2 = {"source": "aws.rds", "severity": {"level": "high"}, "signal_type": "incident", "content": {"title": "RDS snapshot complete", "raw_detail": ""}, "context": {}}
        assert _signal_matches_subscription(signal2, sub_filter) is False

    def test_empty_filter_no_match(self):
        """Empty filter should NOT match anything."""
        from src.persona.audience_router import _signal_matches_subscription

        signal = {"source": "aws.ec2", "severity": {"level": "high"}, "signal_type": "incident", "content": {"title": "x", "raw_detail": ""}, "context": {}}
        assert _signal_matches_subscription(signal, {}) is False

    def test_combined_filters_and_logic(self):
        """All filter fields must match (AND logic)."""
        from src.persona.audience_router import _signal_matches_subscription

        signal = {"source": "aws.rds", "severity": {"level": "high"}, "signal_type": "incident", "content": {"title": "RDS failover", "raw_detail": ""}, "context": {"region": "ap-southeast-1", "tags": {"Environment": "production"}}}
        sub_filter = {"sources": ["aws.rds"], "severity_min": "medium", "regions": ["ap-southeast-1"], "tags": {"Environment": "production"}}

        assert _signal_matches_subscription(signal, sub_filter) is True

        # Change region to mismatch
        signal2 = {**signal, "context": {"region": "us-east-1", "tags": {"Environment": "production"}}}
        assert _signal_matches_subscription(signal2, sub_filter) is False
