"""Unit tests for the escalation handler."""
import json
import os
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime


@pytest.fixture(autouse=True)
def env_vars():
    """Set required environment variables."""
    with patch.dict(os.environ, {
        "DELIVERY_TABLE_NAME": "pulse-delivery-test",
        "PERSONA_WORKFLOW_ARN": "arn:aws:states:ap-southeast-1:123456789012:stateMachine:pulse-persona-workflow-dev",
        "SCHEDULER_GROUP_NAME": "pulse-escalations-dev",
        "STAGE": "dev",
    }):
        yield


@pytest.fixture
def mock_dynamodb():
    """Mock DynamoDB resource and table."""
    with patch("src.delivery.escalation_handler.dynamodb") as mock_ddb:
        table = MagicMock()
        mock_ddb.Table.return_value = table
        yield table


@pytest.fixture
def mock_sfn():
    """Mock Step Functions client."""
    with patch("src.delivery.escalation_handler.sfn_client") as mock:
        yield mock


@pytest.fixture
def mock_scheduler():
    """Mock EventBridge Scheduler client."""
    with patch("src.delivery.escalation_handler.scheduler_client") as mock:
        mock.exceptions = MagicMock()
        mock.exceptions.ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
        yield mock


@pytest.fixture
def sample_signal():
    """Sample signal data for testing."""
    return {
        "signal_id": "01HXY123ABC",
        "source": "aws.cloudwatch",
        "signal_type": "incident",
        "severity": {"level": "high", "score": 75},
        "content": {"title": "EC2 instance unhealthy", "raw_detail": "..."},
        "context": {"account_id": "123456789012", "region": "ap-southeast-1", "resource_arns": []},
        "audience_hint": {"personas": ["sre"], "escalation_chain": ["persona-sre", "persona-cto"], "sla_acknowledge_minutes": 10},
    }


class TestEscalationHandler:
    """Tests for escalation_handler.handler."""

    def test_delivery_already_acknowledged(self, mock_dynamodb, mock_sfn, mock_scheduler, sample_signal):
        """Should not escalate if delivery was already acknowledged."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "deliveryId": "del-01HXY123ABC-persona-ciso-0",
                "acknowledgedAt": "2026-08-07T12:05:00Z",
                "signalId": "01HXY123ABC",
                "personaId": "persona-ciso",
            }
        }

        from src.delivery.escalation_handler import handler

        event = {
            "delivery_id": "del-01HXY123ABC-persona-ciso-0",
            "signal": sample_signal,
            "persona_id": "persona-ciso",
            "escalation_chain": ["persona-sre", "persona-cto"],
            "schedule_name": "pulse-esc-del-01HXY123ABC-persona-ciso-0",
        }

        result = handler(event, None)

        assert result["escalated"] is False
        assert result["reason"] == "already_acknowledged"
        mock_sfn.start_execution.assert_not_called()

    def test_delivery_not_acknowledged_escalates(self, mock_dynamodb, mock_sfn, mock_scheduler, sample_signal):
        """Should escalate to next persona when delivery is not acknowledged."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "deliveryId": "del-01HXY123ABC-persona-ciso-0",
                "signalId": "01HXY123ABC",
                "personaId": "persona-ciso",
                "escalated": False,
                # No acknowledgedAt field
            }
        }
        mock_dynamodb.update_item.return_value = {}

        from src.delivery.escalation_handler import handler

        event = {
            "delivery_id": "del-01HXY123ABC-persona-ciso-0",
            "signal": sample_signal,
            "persona_id": "persona-ciso",
            "escalation_chain": ["persona-ciso", "persona-sre", "persona-cto"],
            "schedule_name": "pulse-esc-del-01HXY123ABC-persona-ciso-0",
        }

        result = handler(event, None)

        assert result["escalated"] is True
        assert result["escalated_to"] == "persona-sre"
        mock_sfn.start_execution.assert_called_once()

        # Verify workflow input targets the next persona
        call_kwargs = mock_sfn.start_execution.call_args.kwargs
        workflow_input = json.loads(call_kwargs["input"])
        assert "persona-sre" in workflow_input["signal"]["audience_hint"]["personas"]

    def test_end_of_escalation_chain(self, mock_dynamodb, mock_sfn, mock_scheduler, sample_signal):
        """Should stop escalating when no more personas in chain."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "deliveryId": "del-01HXY123ABC-persona-cto-0",
                "signalId": "01HXY123ABC",
                "personaId": "persona-cto",
            }
        }
        mock_dynamodb.update_item.return_value = {}

        from src.delivery.escalation_handler import handler

        event = {
            "delivery_id": "del-01HXY123ABC-persona-cto-0",
            "signal": sample_signal,
            "persona_id": "persona-cto",
            "escalation_chain": ["persona-ciso", "persona-sre", "persona-cto"],
            "schedule_name": "pulse-esc-del-01HXY123ABC-persona-cto-0",
        }

        result = handler(event, None)

        assert result["escalated"] is True
        assert result["reason"] == "end_of_chain"
        mock_sfn.start_execution.assert_not_called()

    def test_missing_delivery_id(self, mock_dynamodb, mock_sfn, mock_scheduler):
        """Should return 400 when delivery_id is missing."""
        from src.delivery.escalation_handler import handler

        result = handler({}, None)

        assert result["statusCode"] == 400
        assert result["escalated"] is False

    def test_delivery_record_not_found(self, mock_dynamodb, mock_sfn, mock_scheduler, sample_signal):
        """Should return 404 when delivery record doesn't exist."""
        mock_dynamodb.get_item.return_value = {}

        from src.delivery.escalation_handler import handler

        event = {
            "delivery_id": "del-nonexistent",
            "signal": sample_signal,
            "persona_id": "persona-ciso",
            "escalation_chain": ["persona-sre"],
            "schedule_name": "pulse-esc-del-nonexistent",
        }

        result = handler(event, None)

        assert result["statusCode"] == 404
        assert result["escalated"] is False

    def test_schedule_cleanup_after_ack(self, mock_dynamodb, mock_sfn, mock_scheduler, sample_signal):
        """Should delete the schedule after acknowledging."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "deliveryId": "del-01HXY123ABC-persona-ciso-0",
                "acknowledgedAt": "2026-08-07T12:05:00Z",
            }
        }

        from src.delivery.escalation_handler import handler

        event = {
            "delivery_id": "del-01HXY123ABC-persona-ciso-0",
            "signal": sample_signal,
            "persona_id": "persona-ciso",
            "escalation_chain": [],
            "schedule_name": "pulse-esc-del-01HXY123ABC-persona-ciso-0",
        }

        handler(event, None)

        mock_scheduler.delete_schedule.assert_called_once_with(
            Name="pulse-esc-del-01HXY123ABC-persona-ciso-0",
            GroupName="pulse-escalations-dev",
        )
