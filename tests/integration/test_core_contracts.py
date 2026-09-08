"""Regression coverage at real service-schema boundaries, with no live AWS calls."""
import base64
import hashlib
import json
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from moto import mock_aws

from shared.models import SignalEvent


@pytest.fixture
def signal():
    return SignalEvent(source="custom.test", signal_type="incident",
                       severity={"level": "high", "score": 75},
                       content={"title": "CPU <critical>", "structured_data": {"fraction": 0.75}},
                       context={"account_id": "123456789012", "resource_arns": ["arn:aws:ec2:us-east-1:123456789012:instance/i-1"]})


@pytest.fixture
def aws_tables(monkeypatch):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        definitions = {
            "signals": [("signalId", "HASH"), ("ingestedAt", "RANGE")],
            "correlations": [("groupId", "HASH")],
            "personas": [("personaId", "HASH")],
            "deliveries": [("deliveryId", "HASH")],
        }
        tables = {}
        for name, keys in definitions.items():
            tables[name] = ddb.create_table(TableName=name,
                KeySchema=[{"AttributeName": k, "KeyType": kind} for k, kind in keys],
                AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
                BillingMode="PAY_PER_REQUEST")
        for key, value in {"SIGNAL_TABLE_NAME": "signals", "CORRELATION_TABLE_NAME": "correlations",
                           "PERSONA_TABLE_NAME": "personas", "DELIVERY_TABLE_NAME": "deliveries",
                           "SIGNAL_STREAM_NAME": "signals", "PERSONA_WORKFLOW_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
                           "CALLBACK_API_URL": "https://example.test/dev/", "SES_DOMAIN": "example.test"}.items():
            monkeypatch.setenv(key, value)
        yield ddb, tables


def test_publish_correlate_route_transform_email_and_feedback(signal, aws_tables):
    from src.ingestion import publish_handler
    from src.intelligence import correlator
    from src.persona import audience_router, content_transformer
    from src.delivery import email_sender, action_callback
    ddb, tables = aws_tables
    stream, workflow, ses = MagicMock(), MagicMock(), MagicMock()
    modules = [publish_handler, correlator, audience_router, content_transformer, email_sender, action_callback]
    from contextlib import ExitStack
    with ExitStack() as patches:
        for module in modules:
            patches.enter_context(patch.object(module, "dynamodb", ddb))
        patches.enter_context(patch.object(publish_handler, "kinesis", stream))
        patches.enter_context(patch.object(correlator, "sfn_client", workflow))
        patches.enter_context(patch.object(email_sender, "ses", ses))
        patches.enter_context(patch.object(content_transformer, "transform_for_persona", return_value="A <script>alert(1)</script>"))
        request = {k: v for k, v in signal.to_event().items() if k in ('source', 'signal_type', 'severity', 'content', 'context', 'audience_hint', 'correlation')}
        response = publish_handler.handler({"requestContext": {"identity": {"accountId": "123456789012"}}, "body": json.dumps(request)}, None)
        assert response["statusCode"] == 201
        from src.ingestion import outbox
        from boto3.dynamodb.types import TypeSerializer
        row = tables['signals'].scan()['Items'][0]
        with patch.object(outbox, 'kinesis', stream):
            result = outbox.handler({'Records': [{'eventName': 'INSERT', 'dynamodb': {'SequenceNumber': '1', 'NewImage': {k: TypeSerializer().serialize(v) for k, v in row.items()}}}]}, None)
        assert result['batchItemFailures'] == []
        wire = json.loads(stream.put_record.call_args.kwargs["Data"])
        row = tables["signals"].get_item(Key={"signalId": wire["signal_id"], "ingestedAt": wire["ingested_at"]})["Item"]
        assert row["content"]["structured_data"]["fraction"] == Decimal("0.75")
        record = {"kinesis": {"data": base64.b64encode(json.dumps(wire).encode()).decode(), "sequenceNumber": "1"}}
        assert correlator.handler({"Records": [record]}, None)["batchItemFailures"] == []
        wf_input = json.loads(workflow.start_execution.call_args.kwargs["input"])
        assert wf_input["signal"]["status"] == "correlated"
        tables["personas"].put_item(Item={"personaId": "persona-sre", "roleTemplate": "sre", "languageLevel": "technical_summary",
            "members": [{"principalId": "sre@example.test", "channels": ["email"]}],
            "deliveryPreferences": {"channels": ["email"]}})
        routed = audience_router.handler(wf_input, None)
        assert routed["persona_ids"] == ["persona-sre"]
        transformed = content_transformer.handler(routed, None)
        delivery = email_sender.handler({"signal": transformed["signal"], "delivery": transformed["transformations"][0]}, None)
        assert delivery["delivered"] is True
        html = ses.send_email.call_args.kwargs["Message"]["Body"]["Html"]["Data"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        import re
        url = re.search(r'href="([^"]+acknowledge[^\"]+)"', html)[1]
        assert "/dev//" not in url
        token = parse_qs(urlsplit(url).query)["token"][0]
        did = delivery["delivery_ids"][0]
        event = {"pathParameters": {"deliveryId": did, "action": "acknowledge"}, "queryStringParameters": {"token": token}, "httpMethod": "GET"}
        assert action_callback.handler(event, None)["statusCode"] == 200
        assert "acknowledgedAt" not in tables["deliveries"].get_item(Key={"deliveryId": did})["Item"]
        event["httpMethod"] = "POST"
        assert action_callback.handler(event, None)["statusCode"] == 200
        row = tables["deliveries"].get_item(Key={"deliveryId": did})["Item"]
        assert row["feedback"] == "useful" and row["acknowledgedAt"]
        assert action_callback.handler(event, None)["statusCode"] == 409


@pytest.mark.parametrize("body", ["{", "[]", "null", '{"source":"x"}', '{"source":"x","signal_type":"wrong","severity":{},"content":{}}'])
def test_publish_invalid_payload_is_client_error(body):
    from src.ingestion import publish_handler
    with patch.object(publish_handler, "kinesis") as stream:
        assert publish_handler.handler({"requestContext": {"identity": {"accountId": "123456789012"}}, "body": body}, None)["statusCode"] == 400
        stream.put_record.assert_not_called()


def test_prediction_decimal_storage_and_json_stream(signal, aws_tables):
    from src.intelligence import predictor
    _, tables = aws_tables
    with patch.object(predictor, "kinesis") as stream:
        predictor._publish_prediction(signal, "signals", tables["signals"])
    assert tables["signals"].scan()["Items"][0]["content"]["structured_data"]["fraction"] == Decimal("0.75")
    stream.put_record.assert_not_called()
    assert tables["signals"].scan()["Count"] == 1


def test_native_securityhub_keeps_all_findings(aws_tables):
    from src.ingestion import org_forwarder
    ddb, tables = aws_tables
    with patch.object(org_forwarder, "dynamodb", ddb), patch.object(org_forwarder, "kinesis") as stream:
        result = org_forwarder.handler({"source": "aws.securityhub", "account": "123456789012", "region": "us-east-1",
            "detail": {"findings": [{"Title": title, "Severity": {"Label": "HIGH"}, "Resources": []} for title in ["One", "Two"]]}}, None)
    assert len(result["signalIds"]) == 2 and stream.put_record.call_count == 0
    assert tables["signals"].scan()["Count"] == 2


def test_cloudwatch_alarm_preserves_state_and_top_level_arn(aws_tables):
    from src.ingestion import org_forwarder
    ddb, tables = aws_tables
    with patch.object(org_forwarder, "dynamodb", ddb), patch.object(org_forwarder, "kinesis") as stream:
        org_forwarder.handler({"source": "aws.cloudwatch", "detail-type": "CloudWatch Alarm State Change",
            "resources": ["arn:aws:cloudwatch:us-east-1:123456789012:alarm:cpu"],
            "detail": {"alarmName": "cpu", "state": {"value": "OK", "reason": "Recovered"}}}, None)
    stream.put_record.assert_not_called()
    wire = tables["signals"].scan()["Items"][0]
    assert wire["severity"]["level"] == "informational"
    assert wire["signal_type"] == "lifecycle"
    assert wire["context"]["resource_arns"][0].endswith(":cpu")


@pytest.mark.parametrize("provider", ["datadog", "pagerduty", "servicenow"])
def test_webhook_unconfigured_rejects_before_writes(provider, monkeypatch):
    import importlib
    module = importlib.import_module(f"src.ingestion.webhook_adapters.{provider}")
    for key in ["DATADOG_WEBHOOK_API_KEY", "PAGERDUTY_WEBHOOK_SECRET", "SERVICENOW_WEBHOOK_USER", "SERVICENOW_WEBHOOK_PASS"]:
        monkeypatch.delenv(key, raising=False)
    with patch.object(module, "dynamodb") as ddb, patch.object(module, "kinesis") as stream:
        assert module.handler({"body": "{}", "headers": {}}, None)["statusCode"] == 401
        ddb.Table.assert_not_called()
        stream.put_record.assert_not_called()


@pytest.mark.parametrize("action", ["acknowledge", "escalate", "suppress"])
@pytest.mark.parametrize("token_kind", ["missing", "wrong", "expired", "other_delivery"])
def test_callback_rejects_invalid_capabilities(action, token_kind, aws_tables):
    from src.delivery import action_callback
    ddb, tables = aws_tables
    token = "correct-secret"
    tables["deliveries"].put_item(Item={"deliveryId": "del-1", "actionTokenHash": hashlib.sha256(token.encode()).hexdigest(),
        "actionTokenExpiresAt": int(time.time()) + (60 if token_kind != "expired" else -60)})
    did = "del-2" if token_kind == "other_delivery" else "del-1"
    given = "" if token_kind == "missing" else ("wrong" if token_kind == "wrong" else token)
    with patch.object(action_callback, "dynamodb", ddb):
        response = action_callback.handler({"httpMethod": "POST", "pathParameters": {"deliveryId": did, "action": action},
            "queryStringParameters": {"token": given}}, None)
    assert response["statusCode"] == 403
    assert "actionAt" not in tables["deliveries"].get_item(Key={"deliveryId": "del-1"})["Item"]
    assert tables["deliveries"].scan()["Count"] == 1


@pytest.mark.parametrize("source,severity", [("datadog", "low"), ("aws.cloudwatch", "critical"), ("aws.cloudwatch", "high")])
def test_learned_suppression_cannot_hide_unrelated_or_urgent_alerts(source, severity):
    from src.learning.suppression_model import should_suppress
    rules = [{"source": "learned", "pattern": {"source_key": "aws.cloudwatch"}, "confidence": Decimal("1")}]
    assert not should_suppress({"source": source, "severity": {"level": severity}}, {"suppressionRules": rules})


def test_legacy_global_suppression_is_ignored():
    from src.learning.suppression_model import should_suppress
    assert not should_suppress({"source": "datadog", "severity": {"level": "low"}},
        {"suppressionRules": [{"source": "learned", "pattern": {"source_key": "all"}, "confidence": 1}]})


def test_learned_rule_persists_with_dynamodb_numeric_types(aws_tables):
    from src.learning.feedback_processor import _create_suppression_rule
    from src.learning.suppression_model import should_suppress
    _, tables = aws_tables
    tables["personas"].put_item(Item={"personaId": "persona-sre"})
    _create_suppression_rule(tables["personas"], "persona-sre", "datadog", 3)
    persona = tables["personas"].get_item(Key={"personaId": "persona-sre"})["Item"]
    assert next(iter(persona["learnedSuppressions"].values()))["confidence"] == Decimal("0.5")
    assert should_suppress({"source": "datadog", "severity": {"level": "low"}}, persona)


def test_escalation_routes_only_to_designated_persona(aws_tables):
    from src.persona import audience_router
    ddb, tables = aws_tables
    for pid in ["persona-sre", "persona-ciso", "persona-cto"]:
        tables["personas"].put_item(Item={"personaId": pid})
    with patch.object(audience_router, "dynamodb", ddb):
        result = audience_router.handler({"signal": {"_escalation": True, "severity": {"level": "critical"}, "signal_type": "finding",
            "audience_hint": {"personas": ["persona-cto"]}}}, None)
    assert result["persona_ids"] == ["persona-cto"]


def test_channel_opt_out_is_respected(signal, aws_tables):
    from src.persona import content_transformer
    ddb, tables = aws_tables
    tables["personas"].put_item(Item={"personaId": "persona-sre", "members": [{"principalId": "sre@test", "channels": ["email"]}],
        "deliveryPreferences": {"channels": ["email", "slack"]}})
    with patch.object(content_transformer, "dynamodb", ddb), patch.object(content_transformer, "transform_for_persona", return_value="Notice"):
        result = content_transformer.handler({"signal": signal.to_event(), "persona_ids": ["persona-sre"]}, None)
    assert [entry["channel"] for entry in result["transformations"]] == ["email"]


@pytest.mark.parametrize("model", ["amazon.nova-pro-v1:0", "anthropic.claude-sonnet-4-20250514-v1:0"])
def test_bedrock_request_uses_converse_schema(model):
    from shared import bedrock_client
    from botocore.stub import Stubber
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    with Stubber(client) as stub, patch.object(bedrock_client, "get_client", return_value=client):
        stub.add_response("converse", {"output": {"message": {"role": "assistant", "content": [{"text": "hello"}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}},
            {"modelId": model, "messages": [{"role": "user", "content": [{"text": "test"}]}], "inferenceConfig": {"maxTokens": 1024, "temperature": 0.3}})
        assert bedrock_client.invoke_model("test", model_id=model) == "hello"


def test_sdk_includes_api_key_and_encodes_query():
    from pulse.client import PulseClient
    client = PulseClient("https://api.test/dev", api_key="usage-key")
    response = MagicMock(status_code=200)
    response.json.return_value = {"deliveries": []}
    with patch.object(client._session, "request", return_value=response) as request:
        client.list_deliveries(next_token="a+b/c==")
    sent = request.call_args.kwargs
    assert sent["headers"]["x-api-key"] == "usage-key"
    assert sent["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert parse_qs(urlsplit(sent["url"]).query)["nextToken"] == ["a+b/c=="]


def test_transform_errors_are_not_reported_as_success(signal, aws_tables):
    from src.persona import content_transformer
    ddb, tables = aws_tables
    tables["personas"].put_item(Item={"personaId": "persona-sre"})
    with patch.object(content_transformer, "dynamodb", ddb), patch.object(content_transformer, "transform_for_persona", side_effect=RuntimeError("Bedrock unavailable")):
        with pytest.raises(RuntimeError, match="Bedrock unavailable"):
            content_transformer.handler({"signal": signal.to_event(), "persona_ids": ["persona-sre"]}, None)


def test_scheduler_creation_errors_fail_the_task(signal):
    from src.delivery import schedule_escalation
    from botocore.exceptions import ClientError
    real_exceptions = boto3.client("scheduler", region_name="us-east-1").exceptions
    with patch.object(schedule_escalation, "scheduler_client") as scheduler:
        scheduler.exceptions = real_exceptions
        scheduler.create_schedule.side_effect = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "CreateSchedule")
        with pytest.raises(ClientError):
            schedule_escalation.handler({"signal": signal.to_event(), "delivery_ids": ["del-1"],
                "delivery": {"persona_id": "persona-sre", "escalation_after_minutes": 1, "escalation_chain": ["persona-cto"]}}, None)


def test_escalation_failure_does_not_delete_retry_schedule(signal, aws_tables):
    from src.delivery import escalation_handler
    from botocore.exceptions import ClientError
    ddb, tables = aws_tables
    tables["deliveries"].put_item(Item={"deliveryId": "del-1", "personaId": "persona-sre"})
    with patch.object(escalation_handler, "dynamodb", ddb), patch.object(escalation_handler, "sfn_client") as workflow, patch.object(escalation_handler, "scheduler_client") as scheduler:
        workflow.exceptions = boto3.client("stepfunctions", region_name="us-east-1").exceptions
        workflow.start_execution.side_effect = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "StartExecution")
        with pytest.raises(ClientError):
            escalation_handler.handler({"delivery_id": "del-1", "signal": signal.to_event(), "persona_id": "persona-sre",
                "escalation_chain": ["persona-cto"], "schedule_name": "schedule-1"}, None)
        scheduler.delete_schedule.assert_not_called()


def test_workflow_name_is_stable_for_record_retry(signal):
    from src.intelligence import correlator
    with patch.object(correlator, "sfn_client") as workflow:
        correlator._start_persona_workflow("arn:test", signal.to_event(), signal.signal_id)
        first = workflow.start_execution.call_args.kwargs["name"]
        correlator._start_persona_workflow("arn:test", signal.to_event(), signal.signal_id)
        assert first == workflow.start_execution.call_args.kwargs["name"]


@pytest.mark.parametrize("filter_value", [{"regions": ["us-east-1"]}, {"signal_types": ["incident"]}])
def test_missing_fields_do_not_match_restricted_subscriptions(filter_value):
    from src.persona.audience_router import _signal_matches_subscription
    assert not _signal_matches_subscription({}, filter_value)
