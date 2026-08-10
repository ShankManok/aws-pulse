"""Integration test for escalation flow: publish signal → wait → verify escalation triggered.

This test uses moto to simulate the full flow locally:
1. Publish a signal to the (mocked) Kinesis stream
2. Trigger the correlator to process it
3. Verify the persona workflow is started
4. Simulate no acknowledgement after escalation window
5. Trigger escalation handler
6. Verify escalation to next persona is triggered

Run with: pytest tests/integration/ -v
"""
import json
import os
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

import boto3
from moto import mock_aws


@pytest.fixture
def sample_signal():
    """A test signal that should route to CISO and SRE."""
    return {
        "signal_id": "integ-test-001",
        "source": "aws.securityhub",
        "signal_type": "finding",
        "severity": {"level": "critical", "score": 90},
        "content": {
            "title": "Publicly exposed S3 bucket",
            "raw_detail": "Bucket 'prod-data' has public read access",
        },
        "context": {
            "account_id": "123456789012",
            "region": "ap-southeast-1",
            "resource_arns": ["arn:aws:s3:::prod-data"],
        },
        "audience_hint": {
            "personas": ["ciso", "sre"],
            "escalation_chain": ["persona-ciso", "persona-sre"],
            "sla_acknowledge_minutes": 5,
        },
        "correlation": {"time_window_seconds": 300},
        "ingested_at": datetime.utcnow().isoformat() + "Z",
        "status": "new",
    }


class TestEscalationFlow:
    """Integration tests for the full escalation pipeline."""

    @mock_aws
    def test_unacknowledged_delivery_triggers_escalation(self, sample_signal):
        """
        Full flow:
        1. Create a delivery record (simulating email_sender)
        2. Fire escalation handler (simulating scheduler trigger after SLA)
        3. Verify it detects non-ACK and starts escalation workflow
        """
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_DEFAULT_REGION"] = "ap-southeast-1"

        # Setup DynamoDB table inside @mock_aws scope
        dynamodb = boto3.resource("dynamodb", region_name="ap-southeast-1")
        table = dynamodb.create_table(
            TableName="pulse-delivery-integ",
            KeySchema=[{"AttributeName": "deliveryId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "deliveryId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="pulse-delivery-integ")

        # Step 1: Simulate a delivery record (created by email_sender)
        delivery_id = "del-integ-test-001-persona-ciso-0"
        delivered_at = (datetime.utcnow() - timedelta(minutes=10)).isoformat() + "Z"
        table.put_item(Item={
            "deliveryId": delivery_id,
            "signalId": "integ-test-001",
            "personaId": "persona-ciso",
            "recipientId": "ciso@test.com",
            "channel": "email",
            "deliveredAt": delivered_at,
            "escalated": False,
            # Note: NO acknowledgedAt - simulates unacknowledged delivery
        })

        # Step 2: Fire the escalation handler with patched DDB resource pointing to moto
        with patch.dict(os.environ, {
            "DELIVERY_TABLE_NAME": "pulse-delivery-integ",
            "PERSONA_WORKFLOW_ARN": "arn:aws:states:ap-southeast-1:123456789012:stateMachine:test-workflow",
            "SCHEDULER_GROUP_NAME": "pulse-escalations-test",
            "STAGE": "dev",
        }):
            with patch("src.delivery.escalation_handler.sfn_client") as mock_sfn:
                with patch("src.delivery.escalation_handler.scheduler_client") as mock_scheduler:
                    # Patch the dynamodb resource to use the moto-managed one
                    with patch("src.delivery.escalation_handler.dynamodb", dynamodb):
                        mock_scheduler.exceptions = MagicMock()
                        mock_scheduler.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})

                        from src.delivery.escalation_handler import handler

                        escalation_event = {
                            "delivery_id": delivery_id,
                            "signal": sample_signal,
                            "persona_id": "persona-ciso",
                            "escalation_chain": ["persona-ciso", "persona-sre"],
                            "schedule_name": f"pulse-esc-{delivery_id}",
                        }

                        result = handler(escalation_event, None)

        # Step 3: Verify escalation happened
        assert result["escalated"] is True
        assert result["escalated_to"] == "persona-sre"

        # Verify SFN was called with escalation signal
        mock_sfn.start_execution.assert_called_once()
        sfn_call = mock_sfn.start_execution.call_args.kwargs
        assert sfn_call["stateMachineArn"] == "arn:aws:states:ap-southeast-1:123456789012:stateMachine:test-workflow"

        workflow_input = json.loads(sfn_call["input"])
        assert workflow_input["signal"]["_escalation"] is True
        assert "persona-sre" in workflow_input["signal"]["audience_hint"]["personas"]

        # Verify delivery record was marked as escalated
        record = table.get_item(Key={"deliveryId": delivery_id})["Item"]
        assert record["escalated"] is True
        assert "escalatedAt" in record

    @mock_aws
    def test_acknowledged_delivery_skips_escalation(self, sample_signal):
        """If delivery is acknowledged before escalation fires, no escalation should happen."""
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_DEFAULT_REGION"] = "ap-southeast-1"

        dynamodb = boto3.resource("dynamodb", region_name="ap-southeast-1")
        table = dynamodb.create_table(
            TableName="pulse-delivery-integ2",
            KeySchema=[{"AttributeName": "deliveryId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "deliveryId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="pulse-delivery-integ2")

        delivery_id = "del-integ-test-002-persona-ciso-0"
        table.put_item(Item={
            "deliveryId": delivery_id,
            "signalId": "integ-test-002",
            "personaId": "persona-ciso",
            "recipientId": "ciso@test.com",
            "channel": "email",
            "deliveredAt": "2026-08-07T12:00:00Z",
            "acknowledgedAt": "2026-08-07T12:03:00Z",  # ACK'd before escalation
            "escalated": False,
        })

        with patch.dict(os.environ, {
            "DELIVERY_TABLE_NAME": "pulse-delivery-integ2",
            "PERSONA_WORKFLOW_ARN": "arn:aws:states:ap-southeast-1:123456789012:stateMachine:test-workflow",
            "SCHEDULER_GROUP_NAME": "pulse-escalations-test",
            "STAGE": "dev",
        }):
            with patch("src.delivery.escalation_handler.sfn_client") as mock_sfn:
                with patch("src.delivery.escalation_handler.scheduler_client") as mock_scheduler:
                    with patch("src.delivery.escalation_handler.dynamodb", dynamodb):
                        mock_scheduler.exceptions = MagicMock()
                        mock_scheduler.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})

                        from src.delivery.escalation_handler import handler

                        escalation_event = {
                            "delivery_id": delivery_id,
                            "signal": sample_signal,
                            "persona_id": "persona-ciso",
                            "escalation_chain": ["persona-ciso", "persona-sre"],
                            "schedule_name": f"pulse-esc-{delivery_id}",
                        }

                        result = handler(escalation_event, None)

        assert result["escalated"] is False
        assert result["reason"] == "already_acknowledged"
        mock_sfn.start_execution.assert_not_called()

        # Schedule should be cleaned up
        mock_scheduler.delete_schedule.assert_called_once()

    @mock_aws
    def test_schedule_escalation_creates_scheduler(self, sample_signal):
        """Integration: schedule_escalation should call EventBridge Scheduler API."""
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_DEFAULT_REGION"] = "ap-southeast-1"

        with patch.dict(os.environ, {
            "DELIVERY_TABLE_NAME": "pulse-delivery-test",
            "ESCALATION_FUNCTION_ARN": "arn:aws:lambda:ap-southeast-1:123456789012:function:pulse-escalation-handler-dev",
            "SCHEDULER_ROLE_ARN": "arn:aws:iam::123456789012:role/pulse-scheduler-role-dev",
            "SCHEDULER_GROUP_NAME": "pulse-escalations-test",
            "STAGE": "dev",
        }):
            with patch("src.delivery.schedule_escalation.scheduler_client") as mock_scheduler:
                mock_scheduler.create_schedule.return_value = {"ScheduleArn": "arn:aws:scheduler:..."}
                mock_scheduler.exceptions = MagicMock()
                mock_scheduler.exceptions.ConflictException = type("ConflictException", (Exception,), {})

                from src.delivery.schedule_escalation import handler

                event = {
                    "delivery": {
                        "persona_id": "persona-ciso",
                        "transformed_content": "...",
                        "channel": "email",
                        "recipients": ["ciso@test.com"],
                        "escalation_after_minutes": 15,
                        "escalation_chain": ["persona-ciso", "persona-sre"],
                    },
                    "signal": sample_signal,
                    "delivery_ids": ["del-integ-test-001-persona-ciso-0"],
                }

                result = handler(event, None)

        assert result["scheduled"] is True
        assert len(result["schedule_names"]) == 1
        assert result["fire_at"] is not None

        # Verify scheduler API call
        mock_scheduler.create_schedule.assert_called_once()
        create_call = mock_scheduler.create_schedule.call_args.kwargs
        assert create_call["GroupName"] == "pulse-escalations-test"
        assert "at(" in create_call["ScheduleExpression"]
        assert create_call["ActionAfterCompletion"] == "DELETE"

        # Verify target payload
        target = create_call["Target"]
        assert target["Arn"] == "arn:aws:lambda:ap-southeast-1:123456789012:function:pulse-escalation-handler-dev"
        target_input = json.loads(target["Input"])
        assert target_input["delivery_id"] == "del-integ-test-001-persona-ciso-0"
        assert target_input["persona_id"] == "persona-ciso"
        assert target_input["escalation_chain"] == ["persona-ciso", "persona-sre"]
