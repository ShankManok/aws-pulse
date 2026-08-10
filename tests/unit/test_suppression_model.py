"""Unit tests for the suppression model."""
import os
import pytest
from unittest.mock import patch
from datetime import datetime, timedelta


@pytest.fixture(autouse=True)
def env_vars():
    with patch.dict(os.environ, {
        "PERSONA_TABLE_NAME": "pulse-personas-test",
        "DELIVERY_TABLE_NAME": "pulse-delivery-test",
        "STAGE": "dev",
    }):
        yield


class TestShouldSuppress:

    def test_no_rules_returns_false(self):
        """No suppression rules means no suppression."""
        from src.learning.suppression_model import should_suppress

        signal = {"source": "aws.cloudwatch", "severity": {"level": "high"}, "signal_type": "incident"}
        persona = {"personaId": "persona-sre", "suppressionRules": []}

        assert should_suppress(signal, persona) is False

    def test_missing_rules_key_returns_false(self):
        """Persona without suppressionRules key should not suppress."""
        from src.learning.suppression_model import should_suppress

        signal = {"source": "aws.cloudwatch", "severity": {"level": "high"}, "signal_type": "incident"}
        persona = {"personaId": "persona-sre"}

        assert should_suppress(signal, persona) is False

    def test_learned_rule_above_confidence_suppresses(self):
        """Learned rule with confidence >= 0.5 should suppress."""
        from src.learning.suppression_model import should_suppress

        signal = {"source": "aws.cloudwatch", "severity": {"level": "low"}, "signal_type": "incident"}
        persona = {
            "personaId": "persona-sre",
            "suppressionRules": [{
                "id": "learned-all-123",
                "source": "learned",
                "pattern": {"source_key": "all", "noise_count": 5},
                "confidence": 0.7,
                "expiresAt": (datetime.utcnow() + timedelta(days=10)).isoformat() + "Z",
            }],
        }

        assert should_suppress(signal, persona) is True

    def test_learned_rule_below_confidence_no_suppress(self):
        """Learned rule with confidence < 0.5 should not suppress."""
        from src.learning.suppression_model import should_suppress

        signal = {"source": "aws.cloudwatch", "severity": {"level": "low"}, "signal_type": "incident"}
        persona = {
            "personaId": "persona-sre",
            "suppressionRules": [{
                "id": "learned-all-123",
                "source": "learned",
                "pattern": {"source_key": "all", "noise_count": 2},
                "confidence": 0.3,
                "expiresAt": (datetime.utcnow() + timedelta(days=10)).isoformat() + "Z",
            }],
        }

        assert should_suppress(signal, persona) is False

    def test_expired_rule_skipped(self):
        """Expired rules should be ignored."""
        from src.learning.suppression_model import should_suppress

        signal = {"source": "aws.cloudwatch", "severity": {"level": "low"}, "signal_type": "incident"}
        persona = {
            "personaId": "persona-sre",
            "suppressionRules": [{
                "id": "learned-all-123",
                "source": "learned",
                "pattern": {"source_key": "all", "noise_count": 5},
                "confidence": 0.9,
                "expiresAt": (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z",
            }],
        }

        assert should_suppress(signal, persona) is False

    def test_manual_rule_with_source_filter(self):
        """Manual rule targeting specific source should suppress matching signals."""
        from src.learning.suppression_model import should_suppress

        signal = {"source": "datadog", "severity": {"level": "low"}, "signal_type": "incident"}
        persona = {
            "personaId": "persona-cto",
            "suppressionRules": [{
                "id": "manual-1",
                "source": "manual",
                "pattern": {"source": "datadog"},
                "confidence": 1.0,
            }],
        }

        assert should_suppress(signal, persona) is True

    def test_manual_rule_different_source_no_suppress(self):
        """Manual rule for different source should not suppress."""
        from src.learning.suppression_model import should_suppress

        signal = {"source": "aws.cloudwatch", "severity": {"level": "low"}, "signal_type": "incident"}
        persona = {
            "personaId": "persona-cto",
            "suppressionRules": [{
                "id": "manual-1",
                "source": "manual",
                "pattern": {"source": "datadog"},
                "confidence": 1.0,
            }],
        }

        assert should_suppress(signal, persona) is False

    def test_manual_rule_severity_filter(self):
        """Manual rule with severity filter should match correctly."""
        from src.learning.suppression_model import should_suppress

        signal = {"source": "aws.cloudwatch", "severity": {"level": "informational"}, "signal_type": "incident"}
        persona = {
            "personaId": "persona-ciso",
            "suppressionRules": [{
                "id": "manual-info",
                "source": "manual",
                "pattern": {"severity": "informational"},
                "confidence": 1.0,
            }],
        }

        assert should_suppress(signal, persona) is True

        # Different severity should not match
        signal2 = {"source": "aws.cloudwatch", "severity": {"level": "high"}, "signal_type": "incident"}
        assert should_suppress(signal2, persona) is False

    def test_empty_pattern_no_suppress(self):
        """Rule with empty pattern should not suppress anything."""
        from src.learning.suppression_model import should_suppress

        signal = {"source": "aws.cloudwatch", "severity": {"level": "high"}, "signal_type": "incident"}
        persona = {
            "personaId": "persona-sre",
            "suppressionRules": [{
                "id": "empty-rule",
                "source": "manual",
                "pattern": {},
                "confidence": 1.0,
            }],
        }

        assert should_suppress(signal, persona) is False

    def test_multiple_rules_first_match_wins(self):
        """Should suppress on first matching rule."""
        from src.learning.suppression_model import should_suppress

        signal = {"source": "pagerduty", "severity": {"level": "low"}, "signal_type": "incident"}
        persona = {
            "personaId": "persona-cto",
            "suppressionRules": [
                {
                    "id": "rule-1",
                    "source": "manual",
                    "pattern": {"source": "datadog"},  # Won't match
                    "confidence": 1.0,
                },
                {
                    "id": "rule-2",
                    "source": "manual",
                    "pattern": {"source": "pagerduty"},  # Will match
                    "confidence": 1.0,
                },
            ],
        }

        assert should_suppress(signal, persona) is True
