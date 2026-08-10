"""Unit tests for the audience router."""
import os
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def env_vars():
    """Set required environment variables."""
    with patch.dict(os.environ, {
        "PERSONA_TABLE_NAME": "pulse-personas-test",
        "STAGE": "dev",
    }):
        yield


@pytest.fixture
def mock_dynamodb():
    """Mock DynamoDB resource and table."""
    with patch("src.persona.audience_router.dynamodb") as mock_ddb:
        table = MagicMock()
        mock_ddb.Table.return_value = table
        # By default, all persona lookups succeed
        table.get_item.return_value = {"Item": {"personaId": "exists"}}
        yield table


@pytest.fixture
def make_signal():
    """Factory for creating test signal events."""
    def _make(severity_level="high", signal_type="incident", personas=None, escalation_chain=None):
        signal = {
            "signal_id": "01HXY123ABC",
            "source": "aws.cloudwatch",
            "signal_type": signal_type,
            "severity": {"level": severity_level, "score": 75},
            "content": {"title": "Test signal"},
            "context": {"account_id": "123456789012", "resource_arns": []},
            "audience_hint": {
                "personas": personas or [],
                "escalation_chain": escalation_chain or [],
                "sla_acknowledge_minutes": 10,
            },
        }
        return signal
    return _make


class TestAudienceRouter:
    """Tests for audience_router.handler severity routing logic."""

    def test_critical_routes_to_all_personas(self, mock_dynamodb, make_signal):
        """Critical severity should route to CISO, SRE, and CTO."""
        from src.persona.audience_router import handler

        event = {"signal": make_signal(severity_level="critical")}
        result = handler(event, None)

        persona_ids = result["persona_ids"]
        assert "persona-ciso" in persona_ids
        assert "persona-sre" in persona_ids
        assert "persona-cto" in persona_ids

    def test_high_routes_to_all_personas(self, mock_dynamodb, make_signal):
        """High severity should route to CISO, SRE, and CTO."""
        from src.persona.audience_router import handler

        event = {"signal": make_signal(severity_level="high")}
        result = handler(event, None)

        persona_ids = result["persona_ids"]
        assert "persona-ciso" in persona_ids
        assert "persona-sre" in persona_ids
        assert "persona-cto" in persona_ids

    def test_medium_routes_to_sre_and_cto(self, mock_dynamodb, make_signal):
        """Medium severity should route to SRE and CTO only."""
        from src.persona.audience_router import handler

        event = {"signal": make_signal(severity_level="medium")}
        result = handler(event, None)

        persona_ids = result["persona_ids"]
        assert "persona-ciso" not in persona_ids
        assert "persona-sre" in persona_ids
        assert "persona-cto" in persona_ids

    def test_low_routes_to_sre_only(self, mock_dynamodb, make_signal):
        """Low severity should route to SRE only."""
        from src.persona.audience_router import handler

        event = {"signal": make_signal(severity_level="low")}
        result = handler(event, None)

        persona_ids = result["persona_ids"]
        assert "persona-ciso" not in persona_ids
        assert "persona-sre" in persona_ids
        assert "persona-cto" not in persona_ids

    def test_informational_routes_to_sre_only(self, mock_dynamodb, make_signal):
        """Informational severity should route to SRE only."""
        from src.persona.audience_router import handler

        event = {"signal": make_signal(severity_level="informational")}
        result = handler(event, None)

        persona_ids = result["persona_ids"]
        assert "persona-sre" in persona_ids
        assert len(persona_ids) == 1

    def test_finding_always_includes_ciso(self, mock_dynamodb, make_signal):
        """Security findings should always route to CISO regardless of severity."""
        from src.persona.audience_router import handler

        event = {"signal": make_signal(severity_level="low", signal_type="finding")}
        result = handler(event, None)

        assert "persona-ciso" in result["persona_ids"]

    def test_recommendation_includes_cto(self, mock_dynamodb, make_signal):
        """Recommendations should always include CTO."""
        from src.persona.audience_router import handler

        event = {"signal": make_signal(severity_level="low", signal_type="recommendation")}
        result = handler(event, None)

        assert "persona-cto" in result["persona_ids"]

    def test_audience_hint_personas_resolved(self, mock_dynamodb, make_signal):
        """Should resolve audience_hint persona names to persona IDs."""
        from src.persona.audience_router import handler

        event = {"signal": make_signal(severity_level="informational", personas=["ciso", "cto"])}
        result = handler(event, None)

        assert "persona-ciso" in result["persona_ids"]
        assert "persona-cto" in result["persona_ids"]

    def test_audience_hint_aliases(self, mock_dynamodb, make_signal):
        """Should resolve common aliases like 'security', 'devops', 'engineering'."""
        from src.persona.audience_router import handler

        event = {"signal": make_signal(severity_level="informational", personas=["security", "devops", "engineering"])}
        result = handler(event, None)

        assert "persona-ciso" in result["persona_ids"]  # security → ciso
        assert "persona-sre" in result["persona_ids"]   # devops → sre
        assert "persona-cto" in result["persona_ids"]   # engineering → cto

    def test_invalid_persona_filtered_out(self, mock_dynamodb, make_signal):
        """Should filter out personas that don't exist in DynamoDB."""
        # Make get_item return empty for non-existent personas
        def side_effect(Key, **kwargs):
            persona_id = Key.get("personaId", "")
            if persona_id == "persona-sre":
                return {"Item": {"personaId": "persona-sre"}}
            return {}

        mock_dynamodb.get_item.side_effect = side_effect

        from src.persona.audience_router import handler

        event = {"signal": make_signal(severity_level="low")}
        result = handler(event, None)

        # Only persona-sre should be valid
        assert "persona-sre" in result["persona_ids"]
        assert all(p == "persona-sre" for p in result["persona_ids"])

    def test_missing_signal_data_returns_empty(self, mock_dynamodb):
        """Should return empty persona list when signal is missing."""
        from src.persona.audience_router import handler

        result = handler({}, None)

        assert result["persona_ids"] == []

    def test_signal_passed_through(self, mock_dynamodb, make_signal):
        """Should include original signal data in the output."""
        from src.persona.audience_router import handler

        signal = make_signal(severity_level="high")
        event = {"signal": signal}
        result = handler(event, None)

        assert result["signal"] == signal
        assert result["signal"]["signal_id"] == "01HXY123ABC"

    def test_deduplication_of_personas(self, mock_dynamodb, make_signal):
        """Should not duplicate personas even if multiple routing strategies match."""
        from src.persona.audience_router import handler

        # CISO hinted + finding type + critical severity = all point to CISO
        event = {"signal": make_signal(severity_level="critical", signal_type="finding", personas=["ciso"])}
        result = handler(event, None)

        # persona-ciso should appear only once
        ciso_count = result["persona_ids"].count("persona-ciso")
        assert ciso_count == 1
