"""Failures must remain visible; filters must never widen on malformed inputs."""
import base64
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


def failure(code='AccessDeniedException'):
    return ClientError({'Error':{'Code':code,'Message':'test failure'}},'Test')


def test_shared_serialization_and_model_validation(monkeypatch):
    from shared import runtime, webhooks, bedrock_client, feedback_actions
    from shared.models import SignalEvent
    from shared.personas import SubscriptionFilter
    assert runtime.dumps({1,2}) == '[1, 2]'
    with pytest.raises(TypeError):
        runtime.dumps(object())
    assert runtime.body({'body':base64.b64encode(b'{"x":1}').decode(),'isBase64Encoded':True}) == {'x':1}
    assert webhooks.decode({'body':'{}'}) == {}
    monkeypatch.setenv('WEBHOOK_SECRET_ARN','test')
    with patch.object(webhooks.boto3,'client') as client:
        client.return_value.get_secret_value.return_value = {'SecretString':'{"key":"secret"}'}
        assert webhooks.secret('key') == 'secret'
    for value in [{},{'sources':[]},{'keywords':[' ']},{'regions':['x']*21}]:
        with pytest.raises(ValueError):
            SubscriptionFilter.model_validate(value)
    signal = SignalEvent(source='a', signal_type='incident',severity={'level':'low','score':10},content={'title':'t'},correlation_group_id='cg')
    assert signal.to_dynamo()['correlationGroupId'] == 'cg'
    feedback_actions.follow_up({'signal':{'severity':{'level':'critical'}}}, 'noise')
    with patch.object(bedrock_client,'_client',None), patch.object(bedrock_client.boto3,'client') as client:
        assert bedrock_client.get_client() is client.return_value
        assert bedrock_client.get_client() is client.return_value
        client.assert_called_once()
    with patch.object(bedrock_client,'invoke_model',return_value='{"level":"low","score":10}'):
        assert bedrock_client.score_severity(signal.to_event())['score'] == 10
        assert bedrock_client.transform_for_persona(signal.to_event(),{'role_template':'sre','language_level':'executive'})


def test_router_authorization_pagination_suppression_and_failed_reads():
    from src.persona import audience_router as router
    assert router._resolve_persona_hint('persona-custom') == 'persona-custom'
    signal = {'signal_id':'s','source':'app','severity':{'level':'low'},'signal_type':'recommendation','context':{'account_id':'1'}}
    with pytest.raises(PermissionError):
        router.handler({'signal':{**signal,'org_id':'other'}},None)
    table = MagicMock()
    table.get_item.return_value = {'Item':{'personaId':'p','suppressionRules':[{'source':'manual','pattern':{'source':'app'}}]}}
    assert router._validate_and_filter_personas(table,['p'],signal) == []
    table.get_item.side_effect = RuntimeError('read failed')
    with pytest.raises(RuntimeError):
        router._validate_and_filter_personas(table,['p'],signal)
    table.scan.side_effect = [{'Items':[{'personaId':'p','orgId':'other'}], 'LastEvaluatedKey':{'personaId':'p'}}, {'Items':[{'personaId':'q','subscriptions':[{'enabled':False},{'id':'sub','filter':{'sources':['app']}}]},{'personaId':'empty'}, {'personaId':'r','subscriptions':[{'filter':{'sources':['other']}}]}]}]
    assert router._check_subscriptions(table,signal,set()) == {'q'}
    table.scan.side_effect = RuntimeError('scan failed')
    with pytest.raises(RuntimeError):
        router._check_subscriptions(table,signal,set())
    for rule in [{'sources':['other']},{'tags':{'env':'prod'}},{'account_ids':['2']},{'unknown':['value']}]:
        assert not router._signal_matches_subscription(signal,rule)
    table.get_item.side_effect = None
    table.get_item.return_value = {'Item':{'personaId':'p'}}
    table.scan.side_effect = None
    table.scan.return_value = {'Items':[{'personaId':'custom','subscriptions':[{'filter':{'sources':['app']}}]}]}
    with patch.object(router,'dynamodb') as ddb:
        ddb.Table.return_value = table
        result = router.handler({'signal':signal},None)
        assert 'persona-cto' in result['persona_ids'] and 'custom' in result['persona_ids']
        assert router.handler({'signal':{**signal,'signal_type':'finding'}},None)['persona_ids']
    table.scan.return_value = {'Items':[{'personaId':'q','subscriptions':[{'filter':{'sources':['app']}}]}]}
    assert router._check_subscriptions(table,signal,{'q'}) == set()


def test_transform_rejects_missing_or_unsupported_configuration():
    from src.persona import content_transformer as transform
    assert transform.handler({},None)['transformations'] == []
    with pytest.raises(PermissionError):
        transform.handler({'signal':{'org_id':'other'}},None)
    with patch.object(transform,'dynamodb') as ddb, patch.object(transform,'transform_for_persona',return_value='text'):
        table = ddb.Table.return_value
        table.get_item.return_value = {}
        assert transform.handler({'signal':{'source':'app'},'persona_ids':['p']},None)['transformations'] == []
        for prefs in [{'channels':['sms']},{'cadence':'daily'},{'quietHours':{'start':'22:00'}}]:
            table.get_item.return_value = {'Item':{'deliveryPreferences':prefs}}
            with pytest.raises(ValueError):
                transform.handler({'signal':{'source':'app'},'persona_ids':['p']},None)
        table.get_item.return_value = {'Item':{'members':[{'principalId':'a','channels':['email']}],'deliveryPreferences':{'escalationAfterMinutes':5}}}
        result = transform.handler({'signal':{'_escalation':True,'audience_hint':{'escalation_chain':['p','q']}},'persona_ids':['p']},None)
        assert result['transformations'][0]['escalation_chain'] == ['p','q']
    assert transform._group_recipients_by_channel([{}, {'principalId':'a','channels':['sms']}],['email']) == {'email':[]}


def test_subscription_checks_persona_before_ai_and_rejects_service_failure():
    from src.persona import subscription_agent as module
    event = {'requestContext':{'identity':{'accountId':'123456789012'}},'pathParameters':{'personaId':'p'},'body':'{"naturalLanguage":"RDS failure"}'}
    assert module.handler({**event,'requestContext':{}},None)['statusCode'] == 403
    with patch.object(module,'dynamodb') as ddb, patch.object(module,'invoke_model') as model:
        table = ddb.Table.return_value
        table.get_item.return_value = {}
        assert module.handler(event,None)['statusCode'] == 404
        model.assert_not_called()
        table.get_item.return_value = {'Item':{'subscriptions':[{}]*100}}
        assert module.handler(event,None)['statusCode'] == 409
        table.get_item.return_value = {'Item':{'personaId':'p'}}
        model.side_effect = RuntimeError('AI unavailable')
        assert module.handler(event,None)['statusCode'] == 500
        table.update_item.assert_not_called()
        model.side_effect = None
        model.return_value = '```json\n{"sources":["aws.rds"]}\n```'
        assert module.handler(event,None)['statusCode'] == 201


def test_predictor_error_and_regression_edges():
    from src.intelligence import predictor as module
    start = datetime.utcnow()
    with patch.object(module,'dynamodb') as ddb, patch.object(module,'cloudwatch') as cw:
        table = ddb.Table.return_value
        table.scan.side_effect = RuntimeError('scan unavailable')
        with pytest.raises(RuntimeError):
            module.handler({},None)
        table.scan.side_effect = None
        table.scan.return_value = {'Items':[{'namespace':'n','metricName':'m'}]}
        cw.get_metric_statistics.side_effect = RuntimeError('metric unavailable')
        with pytest.raises(RuntimeError):
            module.handler({},None)
    assert module._evaluate_predictor({}) is None
    assert module._extrapolate_time_to_breach([],10,'GreaterThanThreshold') is None
    assert module._extrapolate_time_to_breach([(start,1),(start,2)],10,'GreaterThanThreshold') is None
    rising = [(start,1),(start+timedelta(hours=1),2)]
    falling = [(start,10),(start+timedelta(hours=1),9)]
    assert module._extrapolate_time_to_breach(rising,0,'GreaterThanThreshold') == .1
    assert module._extrapolate_time_to_breach(rising,0,'LessThanThreshold') is None
    assert module._extrapolate_time_to_breach(falling,8,'LessThanThreshold') == 1
    assert module._extrapolate_time_to_breach(rising,0,'Invalid') is None
    assert [module._score_from_hours(h) for h in [1,15,30,60]] == [85,70,50,30]


def test_suppression_recalculation_prunes_expired_and_preserves_manual_rules():
    from src.learning import suppression_model as module
    rules = [{'source':'manual','id':'manual','pattern':{'source':'app'}}, {'source':'learned','id':'learned'},
             {'source':'manual','id':'old','expiresAt':'2000-01-01T00:00:00Z'}, {'source':'learned','id':'invalid','expiresAt':'bad'}]
    with patch.object(module,'dynamodb') as ddb:
        table = ddb.Table.return_value
        table.get_item.return_value = {}
        assert module.recalculate_suppression_rules('p') == []
        table.get_item.return_value = {'Item':{'suppressionRules':rules}}
        assert len(module.recalculate_suppression_rules('p')) == 2
        table.update_item.side_effect = RuntimeError('write failed')
        with pytest.raises(RuntimeError):
            module.recalculate_suppression_rules('p')
        table.update_item.side_effect = None
        table.scan.return_value = {'Items':[{'personaId':'p'},{}]}
        assert module.handler({},None)['processed'] == 1
        table.scan.side_effect = RuntimeError('read failed')
        with pytest.raises(RuntimeError):
            module.handler({},None)
    assert not module.should_suppress({'source':'app'}, {'suppressionRules':[{'pattern':{'source':'app'},'expiresAt':'invalid'}]})
    assert not module._matches_rule({'pattern':{'signal_type':'finding'}},'app','low','incident')


def test_feedback_write_failures_are_batch_retries_not_success():
    from src.learning import feedback_processor as module
    image = {key:{'S':value} for key,value in {'feedback':'noise','personaId':'p','signalId':'s','deliveryId':'d'}.items()}
    event = {'Records':[{'eventName':'MODIFY','dynamodb':{'SequenceNumber':'1','NewImage':image}}]}
    with patch.object(module,'dynamodb') as ddb, patch.object(module,'cloudwatch') as cw:
        table = ddb.Table.return_value
        table.query.side_effect = RuntimeError('query failed')
        assert module.handler(event,None)['batchItemFailures'] == [{'itemIdentifier':'1'}]
        table.query.side_effect = None
        table.query.return_value = {'Items':[{'signalId':'1','signalSource':'all'}, {'signalId':'2','signalSource':'app'},{'signalId':'2','signalSource':'app'}, {}]}
        assert module.handler(event,None)['batchItemFailures'] == []
        assert table.update_item.call_args.kwargs["UpdateExpression"].startswith("REMOVE")
        table.update_item.side_effect = RuntimeError('write failed')
        with pytest.raises(RuntimeError):
            module._create_suppression_rule(table,'p','app',3)
        cw.put_metric_data.side_effect = RuntimeError('metric failed')
        module._publish_feedback_metric('p','noise')
    assert module._get_str({'key':'plain'},'key') == 'plain'
