"""Provider formats, callback races and scheduler retry contracts."""
import base64
import hashlib
import hmac
import json
import time
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError


def error(code='AccessDeniedException'):
    return ClientError({'Error':{'Code':code,'Message':'failed'}},'Test')


def test_callback_validation_errors_and_conditional_race(monkeypatch):
    from src.delivery import action_callback as module, email_sender, slack_sender
    monkeypatch.setenv('DELIVERY_TABLE_NAME','table')
    assert module.handler({},None)['statusCode'] == 400
    base = {'pathParameters':{'deliveryId':'d','action':'acknowledge'},'httpMethod':'DELETE'}
    assert module.handler(base,None)['statusCode'] == 405
    with patch.object(module,'parse_qs',side_effect=ValueError()):
        assert module.handler({**base,'httpMethod':'POST','body':'bad'},None)['statusCode'] == 400
    event = {**base,'httpMethod':'POST','queryStringParameters':{'token':'secret'}}
    row = {'actionTokenHash':hashlib.sha256(b'secret').hexdigest(),'actionTokenExpiresAt':int(time.time())+3600}
    with patch.object(module,'dynamodb') as ddb, patch.object(module,'follow_up') as action:
        table = ddb.Table.return_value
        table.get_item.return_value = {'Item':{**row,'orgId':'other'}}
        assert module.handler(event,None)['statusCode'] == 403
        table.get_item.return_value = {'Item':row}
        for failure, status in [(error('ConditionalCheckFailedException'),409),(error(),500),(RuntimeError(),500)]:
            table.update_item.side_effect = failure
            assert module.handler(event,None)['statusCode'] == status
        table.update_item.side_effect = None
        event['pathParameters']['action'] = 'escalate'
        assert module.handler(event,None)['statusCode'] == 200
        assert 'escalationRequestedAt' in table.update_item.call_args.kwargs['UpdateExpression']
        action.assert_called()
    assert email_sender.handler({},None)['delivered'] is False
    with patch.object(slack_sender,'dynamodb'):
        monkeypatch.setenv('SLACK_DESTINATIONS','{}')
        with pytest.raises(ValueError):
            slack_sender.handler({'delivery':{'recipients':['unconfigured']}},None)


def test_escalation_failure_retry_and_chain_preserved(monkeypatch):
    from src.delivery import escalation_handler as module
    monkeypatch.setenv('PERSONA_WORKFLOW_ARN','arn:workflow')
    event = {'delivery_id':'d','persona_id':'p','signal':{'signal_id':'s'},'escalation_chain':['p','q','r']}
    with patch.object(module,'dynamodb') as ddb, patch.object(module,'sfn_client') as sfn, patch.object(module,'scheduler_client') as scheduler:
        sfn.exceptions = boto3.client('stepfunctions').exceptions
        scheduler.exceptions = boto3.client('scheduler').exceptions
        table = ddb.Table.return_value
        table.get_item.side_effect = RuntimeError('read failed')
        with pytest.raises(RuntimeError):
            module.handler(event,None)
        table.get_item.side_effect = None
        table.get_item.return_value = {'Item':{'orgId':'other'}}
        with pytest.raises(PermissionError):
            module.handler(event,None)
        table.get_item.return_value = {'Item':{'deliveryId':'d'}}
        monkeypatch.setenv('PERSONA_WORKFLOW_ARN','')
        with pytest.raises(RuntimeError):
            module.handler(event,None)
        monkeypatch.setenv('PERSONA_WORKFLOW_ARN','arn:workflow')
        table.update_item.side_effect = RuntimeError('write failed')
        with pytest.raises(RuntimeError):
            module.handler(event,None)
        table.update_item.side_effect = None
        sfn.start_execution.side_effect = sfn.exceptions.ExecutionAlreadyExists({'Error':{'Code':'ExecutionAlreadyExists'}},'StartExecution')
        assert module.handler(event,None)['escalated_to'] == 'q'
        sent = json.loads(sfn.start_execution.call_args.kwargs['input'])['signal']
        assert sent['audience_hint']['escalation_chain'] == ['p','q','r']
        assert module._get_next_persona('r',sent['audience_hint']['escalation_chain']) is None
        assert module._get_next_persona('p',[]) is None
        module._cleanup_schedule('')
        scheduler.delete_schedule.side_effect = scheduler.exceptions.ResourceNotFoundException({'Error':{'Code':'ResourceNotFoundException'}},'DeleteSchedule')
        module._cleanup_schedule('schedule')
        scheduler.delete_schedule.side_effect = RuntimeError('cleanup failed')
        module._cleanup_schedule('schedule')


def test_scheduler_skip_and_conflict_replay():
    from src.delivery import schedule_escalation as module
    assert module.handler({},None)['reason'] == 'no_escalation_time'
    assert module.handler({'delivery':{'escalation_after_minutes':1}},None)['reason'] == 'no_deliveries'
    event = {'delivery':{'escalation_after_minutes':1},'delivery_ids':['d']}
    assert module.handler(event,None)['reason'] == 'no_chain'
    event['delivery']['escalation_chain'] = ['p']
    with patch.object(module,'scheduler_client') as scheduler:
        scheduler.exceptions = boto3.client('scheduler').exceptions
        scheduler.create_schedule.side_effect = scheduler.exceptions.ConflictException({'Error':{'Code':'ConflictException'}},'CreateSchedule')
        assert module.handler(event,None)['scheduled'] is True


def test_correlator_update_failure_retries_and_workflow_replay():
    from src.intelligence import correlator as module
    signal = {'signal_id':'s','ingested_at':'2026-01-01T00:00:00Z','context':{'resource_arns':['arn:resource']}}
    def event(signal):
        return {'Records':[{'kinesis':{'sequenceNumber':'1','data':base64.b64encode(json.dumps(signal).encode()).decode()}}]}
    assert module.handler(event({**signal,'org_id':'other'}),None)['batchItemFailures'] == [{'itemIdentifier':'1'}]
    with patch.object(module,'dynamodb') as ddb, patch.object(module,'sfn_client') as sfn:
        table = ddb.Table.return_value
        table.update_item.side_effect = [None,RuntimeError('signal status unavailable')]
        assert module.handler(event(signal),None)['batchItemFailures'] == [{'itemIdentifier':'1'}]
        sfn.exceptions = boto3.client('stepfunctions').exceptions
        sfn.start_execution.side_effect = sfn.exceptions.ExecutionAlreadyExists({'Error':{'Code':'ExecutionAlreadyExists'}},'StartExecution')
        module._start_persona_workflow('arn:workflow',signal,'s')
        sfn.start_execution.side_effect = RuntimeError('workflow unavailable')
        with pytest.raises(RuntimeError):
            module._start_persona_workflow('arn:workflow',signal,'s')


def test_provider_format_edges_and_invalid_bodies(monkeypatch):
    from src.ingestion.webhook_adapters import datadog as dd, pagerduty as pd, servicenow as sn
    for key,value in {'DATADOG_WEBHOOK_API_KEY':'dd','PAGERDUTY_WEBHOOK_SECRET':'pd','SERVICENOW_WEBHOOK_USER':'user','SERVICENOW_WEBHOOK_PASS':'pass'}.items():
        monkeypatch.setenv(key,value)
    assert not dd._validate_api_key('')
    assert not pd._validate_signature('{}','')
    assert not sn._validate_basic_auth('')
    assert not sn._validate_basic_auth('Basic invalid')
    for module,headers in [(dd,{'DD-API-KEY':'dd'}),(sn,{'Authorization':'Basic '+base64.b64encode(b'user:pass').decode()})]:
        assert module.handler({'body':'{','headers':headers},None)['statusCode'] == 400
    def signed(body, encoded=False):
        signature = 'v1='+hmac.new(b'pd',body.encode(),hashlib.sha256).hexdigest()
        return {'body':base64.b64encode(body.encode()).decode() if encoded else body,'headers':{'X-PagerDuty-Signature':signature},'isBase64Encoded':encoded}
    assert pd.handler(signed('{'),None)['statusCode'] == 400
    assert pd.handler(signed('{}',True),None)['statusCode'] == 200
    assert pd.handler({'body':'bad','isBase64Encoded':True},None)['statusCode'] == 400
    signal = dd._normalize_alert({'tags':['resource:arn:aws:s3:::bucket'],'alert_type':'recommendation'})
    assert signal.signal_type.value == 'recommendation' and signal.context.resource_arns == ['arn:aws:s3:::bucket']
    assert sn._normalize_incident({'category':'change','assignment_group':'executive'}).signal_type.value == 'lifecycle'


def test_native_fallback_normalization_handles_event_variants():
    from src.ingestion import org_forwarder as module
    from shared.models import SeverityLevel
    assert module._extract_resource_arns('', {'alarmArn':'arn:a','findings':[{'Resources':[{'Id':'arn:b'},{'Id':'not-an-arn'}]}]}) == ['arn:a','arn:b']
    for source,detail in [('aws.cloudwatch',{'alarmName':'alarm'}),('aws.securityhub',{'findings':[]}),('aws.securityhub',{'findings':[{'Title':'test','Description':'detail'}]}),('aws.config',{'change':'test'})]:
        assert module._extract_content(source,'event',detail)[0]
    for detail in [{'findings':[{'Severity':{'Label':'CRITICAL','Normalized':120}}]},{}]:
        assert module._extract_severity('aws.securityhub',detail,SeverityLevel.MEDIUM)[1] <= 100
    assert module._extract_severity('aws.guardduty',{'severity':5},SeverityLevel.MEDIUM)[1] == 55
    assert module._extract_severity('aws.guardduty',{'severity':1},SeverityLevel.MEDIUM)[1] == 30
    assert module._default_personas_for_source('aws.config') == ['sre']


@pytest.mark.parametrize('provider,payload', [('datadog',{'tags':None}),('servicenow',{'impact':'invalid'}),('pagerduty',{'event':[]})])
def test_malformed_authenticated_provider_payload_returns_400(provider,payload,monkeypatch):
    import importlib
    module = importlib.import_module('src.ingestion.webhook_adapters.'+provider)
    validator = {'datadog':'_validate_api_key','servicenow':'_validate_basic_auth','pagerduty':'_validate_signature'}[provider]
    with patch.object(module,validator,return_value=True), patch.object(module,'persist') as persist:
        assert module.handler({'body':json.dumps(payload)},None)['statusCode'] == 400
        persist.assert_not_called()
