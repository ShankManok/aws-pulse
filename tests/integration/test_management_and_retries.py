"""Service-schema tests for authorization, API completeness and retry durability."""
import base64
import io
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from shared.models import SignalEvent
from shared.runtime import dumps


def api(route, method='GET', data=None, params=None, query=None, account='123456789012'):
    return {'resource': route, 'httpMethod': method, 'body': json.dumps(data or {}),
            'pathParameters': params, 'queryStringParameters': query,
            'requestContext': {'identity': {'accountId': account}}}


def error(code='AccessDeniedException'):
    return ClientError({'Error': {'Code': code, 'Message': 'test failure'}}, 'test')


@pytest.fixture
def db(monkeypatch):
    with mock_aws():
        ddb = boto3.resource('dynamodb')
        schemas = {'SIGNAL': ('signalId', 'ingestedAt'), 'PERSONA': ('personaId',),
                   'DELIVERY': ('deliveryId',), 'ANALYTICS': ('snapshotId',)}
        tables = {}
        for kind, keys in schemas.items():
            indexes = []
            if kind == 'DELIVERY':
                indexes = [('by-signal', 'signalId', 'deliveredAt'), ('by-persona', 'personaId', 'deliveredAt')]
            if kind == 'ANALYTICS':
                indexes = [('by-org-date', 'orgId', 'date')]
            names = set(keys) | {key for _, a, b in indexes for key in (a, b)}
            def schema(keys):
                return [{'AttributeName': key, 'KeyType': 'HASH' if i == 0 else 'RANGE'} for i, key in enumerate(keys)]
            kwargs = {'GlobalSecondaryIndexes': [{'IndexName': index, 'KeySchema': schema((a, b)), 'Projection': {'ProjectionType': 'ALL'}} for index, a, b in indexes]} if indexes else {}
            tables[kind] = ddb.create_table(TableName=kind.lower(), KeySchema=schema(keys),
                AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'} for key in names], BillingMode='PAY_PER_REQUEST', **kwargs)
            monkeypatch.setenv(f'{kind}_TABLE_NAME', kind.lower())
        monkeypatch.setenv('SIGNAL_STREAM_NAME', 'signals')
        monkeypatch.setenv('ACTION_FUNCTION_NAME', 'callback')
        monkeypatch.setenv('SUBSCRIPTION_FUNCTION_NAME', 'subscribe')
        monkeypatch.setenv('ESCALATION_FUNCTION_NAME', 'escalate')
        from src.api import management
        from src.delivery import action_callback, email_sender, slack_sender
        from src.ingestion import publish_handler
        from shared import feedback_actions
        from contextlib import ExitStack
        with ExitStack() as stack:
            for module in [management, action_callback, email_sender, slack_sender, publish_handler, feedback_actions]:
                stack.enter_context(patch.object(module, 'dynamodb', ddb))
            yield tables


@pytest.fixture
def signal():
    return SignalEvent(source='test.app', signal_type='incident', severity={'level': 'low', 'score': 20}, content={'title': 'Failure', 'structured_data': {'fraction': .25}})


def persona():
    return {'name': 'On call', 'roleTemplate': 'sre', 'members': [{'principalId': 'a@example.test', 'channels': ['email']}], 'deliveryPreferences': {'channels': ['email']}}


def test_persona_management_auth_validation_and_optimistic_updates(db):
    from src.api.management import handler
    assert handler(api('/v1/personas', 'POST', persona(), account='bad'), None)['statusCode'] == 403
    assert handler(api('/v1/personas', 'POST', {**persona(), 'orgId': 'other'}), None)['statusCode'] == 403
    assert handler(api('/v1/personas', 'POST', {'name': 'invalid'}), None)['statusCode'] == 400
    created = handler(api('/v1/personas', 'POST', persona()), None)
    assert created['statusCode'] == 201
    pid = json.loads(created['body'])['personaId']
    params = {'personaId': pid}
    route = '/v1/personas/{personaId}'
    assert handler(api(route, 'PUT', {'name': 'Updated', 'version': 1}, params), None)['statusCode'] == 200
    assert handler(api(route, 'PUT', {'name': 'Stale', 'version': 1}, params), None)['statusCode'] == 409
    assert db['PERSONA'].get_item(Key=params)['Item']['name'] == 'Updated'
    assert handler(api(route, 'PUT', {'orgId': 'other'}, params), None)['statusCode'] == 400
    assert handler(api(route, 'PUT', {}, {'personaId': 'missing'}), None)['statusCode'] == 404
    assert handler(api('/unknown'), None)['statusCode'] == 404
    with patch.object(db['PERSONA'].meta.client, 'put_item', side_effect=error()):
        with pytest.raises(ClientError):
            # Patch resource used by handler, not a separate instance's client.
            from src.api import management
            with patch.object(management, 'table', return_value=db['PERSONA']):
                handler(api('/v1/personas', 'POST', persona()), None)


def test_publish_idempotency_is_atomic_and_rejects_payload_reuse(db, signal):
    from src.ingestion.publish_handler import handler
    data = {k: v for k, v in signal.to_event().items() if k in ('source', 'signal_type', 'severity', 'content', 'context')}
    event = api('/v1/signals', 'POST', data)
    event['headers'] = {'Idempotency-Key': 'order-123'}
    first, second = handler(event, None), handler(event, None)
    assert first['statusCode'] == second['statusCode'] == 201
    assert first['body'] == second['body']
    assert db['SIGNAL'].scan()['Count'] == 2  # one signal plus its receipt, atomically
    event['body'] = json.dumps({**data, 'content': {'title': 'different'}})
    assert handler(event, None)['statusCode'] == 400
    assert db['SIGNAL'].scan()['Count'] == 2
    event['headers']['Idempotency-Key'] = 'a' * 257
    assert handler(event, None)['statusCode'] == 400
    from src.api.management import handler as read
    sid = json.loads(first['body'])['signalId']
    row = json.loads(read(api('/v1/signals/{signalId}', params={'signalId': sid}), None)['body'])
    assert row['signalType'] == 'incident' and row['content']['structured_data']['fraction'] == .25
    assert read(api('/v1/signals/{signalId}', params={'signalId': 'missing'}), None)['statusCode'] == 404
    for body, expected in [({**data, 'org_id': 'other'}, 400), ({**data, 'context': {'account_id': 'bad'}}, 403)]:
        assert handler(api('/v1/signals', 'POST', body), None)['statusCode'] == expected
    assert handler(api('/v1/signals', 'POST', data, account=''), None)['statusCode'] == 403


def test_transaction_errors_are_not_misreported_as_duplicates(db, signal):
    from shared.ingest import persist
    for failure in ['AccessDeniedException', 'TransactionCanceledException']:
        with patch('shared.ingest.boto3.client') as client:
            client.return_value.transact_write_items.side_effect = error(failure)
            with pytest.raises(ClientError):
                persist(signal, db['SIGNAL'], 'test')
    assert db['SIGNAL'].scan()['Count'] == 0


def test_outbox_skips_mutations_and_receipts_and_retries_failed_record(db, signal):
    from src.ingestion import outbox
    from boto3.dynamodb.types import TypeSerializer
    row = {**signal.to_dynamo(), 'recordType': 'signal'}
    def record(seq, kind, row):
        return {'eventName': kind, 'dynamodb': {'SequenceNumber': seq, 'NewImage': {k: TypeSerializer().serialize(v) for k, v in row.items()}}}
    records = [record('1', 'MODIFY', row), record('2', 'INSERT', {'recordType': 'receipt'}), record('3', 'INSERT', row)]
    with patch.object(outbox, 'kinesis') as stream:
        stream.put_record.side_effect = error()
        assert outbox.handler({'Records': records}, None) == {'batchItemFailures': [{'itemIdentifier': '3'}]}
        stream.put_record.side_effect = None
        assert outbox.handler({'Records': [records[-1]]}, None)['batchItemFailures'] == []
        sent = json.loads(stream.put_record.call_args.kwargs['Data'])
        assert sent['signal_id'] == signal.signal_id and 'signalId' not in sent
        assert stream.put_record.call_args.kwargs['PartitionKey'].startswith('default:')


def test_delivery_queries_paginate_filter_and_hide_action_credentials(db):
    from src.api.management import handler
    for i in range(5):
        db['DELIVERY'].put_item(Item={'deliveryId': str(i), 'orgId': 'other' if i == 4 else 'default', 'personaId': 'p' if i < 3 else 'q', 'signalId': 's', 'deliveredAt': f'2026-01-0{i+1}', 'actionTokenHash': 'secret', 'signal': {'private': 1}})
    query = {'signalId': 's', 'personaId': 'p', 'limit': '1'}
    found = []
    while True:
        result = handler(api('/v1/deliveries', query=query), None)
        assert result['statusCode'] == 200
        data = json.loads(result['body'])
        found.extend(data['deliveries'])
        if not data.get('nextToken'):
            break
        query['nextToken'] = data['nextToken']
    assert {row['deliveryId'] for row in found} == {'0', '1', '2'}
    assert all('actionTokenHash' not in row and 'signal' not in row for row in found)
    assert handler(api('/v1/deliveries', query={'limit': '0'}), None)['statusCode'] == 400
    assert handler(api('/v1/deliveries', query={'nextToken': 'bad'}), None)['statusCode'] == 400
    assert len(json.loads(handler(api('/v1/deliveries', query={'personaId': 'q'}), None)['body'])['deliveries']) == 1
    assert len(json.loads(handler(api('/v1/deliveries'), None)['body'])['deliveries']) == 4
    wrong = base64.urlsafe_b64encode(dumps({'org': 'other', 'filter': {}, 'key': {}}).encode()).decode()
    assert handler(api('/v1/deliveries', query={'nextToken': wrong}), None)['statusCode'] == 400


def test_feedback_actions_and_analytics_are_tenant_bound(db, signal):
    from src.api import management
    from src.delivery import action_callback
    from shared import feedback_actions
    db['PERSONA'].put_item(Item={'personaId': 'p', 'orgId': 'default'})
    db['DELIVERY'].put_item(Item={'deliveryId': 'd', 'personaId': 'p', 'orgId': 'default', 'signalSource': 'test.app', 'signalType': 'incident', 'signal': signal.to_dynamo()})
    def invoke(**kwargs):
        result = action_callback.handler(json.loads(kwargs['Payload']), None)
        return {'Payload': io.BytesIO(json.dumps(result).encode())}
    with patch.object(management, 'lambda_client') as client:
        client.invoke.side_effect = invoke
        for value in ['useful', 'resolved', 'noise']:
            assert management.handler(api('/v1/feedback', 'POST', {'deliveryId': 'd', 'feedback': value}), None)['statusCode'] == 200
        row = db['DELIVERY'].get_item(Key={'deliveryId': 'd'})['Item']
        assert row['acknowledgedAt'] and row['resolvedAt']
        from src.learning.suppression_model import should_suppress
        assert should_suppress(signal.to_event(), db['PERSONA'].get_item(Key={'personaId': 'p'})['Item'])
        assert management.handler(api('/v1/feedback', 'POST', {'deliveryId': 'missing', 'feedback': 'useful'}), None)['statusCode'] == 404
        assert management.handler(api('/v1/feedback', 'POST', {'deliveryId': 'd', 'feedback': 'bad'}), None)['statusCode'] == 400
        client.invoke.side_effect = None
        client.invoke.return_value = {'FunctionError': 'Unhandled'}
        with pytest.raises(RuntimeError):
            management.handler(api('/v1/feedback', 'POST', {'deliveryId': 'd', 'feedback': 'useful'}), None)
    for data, status in [({'orgId': 'other'},403), ({'orgId':'default','deliveryId':'missing'},404), ({'orgId':'default','deliveryId':'d','feedback':'bad'},400)]:
        assert action_callback.handler({'internalFeedback': data}, None)['statusCode'] == status
    with patch.object(feedback_actions, 'lambda_client') as client:
        client.invoke.return_value = {'Payload': io.BytesIO(b'{"statusCode":200}')}
        feedback_actions.follow_up(row, 'escalate')
        assert json.loads(client.invoke.call_args.kwargs['Payload'])['delivery_id'] == 'd'
        for result in [{'FunctionError': 'Unhandled'}, {'Payload': io.BytesIO(b'{"statusCode":500}')}]:
            client.invoke.return_value = result
            with pytest.raises(RuntimeError):
                feedback_actions.follow_up(row, 'escalate')
    with pytest.raises(ValueError):
        feedback_actions.follow_up({}, 'noise')
    with pytest.raises(PermissionError):
        feedback_actions.follow_up({'signalSource': 'test', 'personaId': 'missing'}, 'noise')
    for org in ['default', 'other']:
        db['ANALYTICS'].put_item(Item={'snapshotId': org, 'orgId': org, 'date': '2026-01-01', 'nrs': Decimal('12.5')})
    assert management.handler(api('/v1/analytics/nrs', query={'orgId': 'other'}), None)['statusCode'] == 403
    data = json.loads(management.handler(api('/v1/analytics/nrs'), None)['body'])
    assert len(data['snapshots']) == 1 and data['snapshots'][0]['nrs'] == 12.5


@pytest.mark.parametrize('channel', ['email', 'slack'])
def test_delivery_replays_preserve_feedback_and_recipient_reordering(db, signal, channel, monkeypatch):
    from src.delivery import email_sender, slack_sender
    module = email_sender if channel == 'email' else slack_sender
    monkeypatch.setenv('CHATBOT_SNS_TOPIC_ARN', 'arn:aws:sns:us-east-1:123456789012:chat')
    monkeypatch.setenv('SLACK_DESTINATIONS', json.dumps({'a@example.test':'arn:aws:sns:us-east-1:123456789012:a','b@example.test':'arn:aws:sns:us-east-1:123456789012:b'}))
    event = {'signal': signal.to_event(), 'delivery': {'persona_id': 'p', 'channel': channel, 'recipients': ['a@example.test','b@example.test'], 'transformed_content':'Message'}}
    with patch.object(module, 'ses' if channel == 'email' else 'sns_client') as sender:
        first = module.handler(event, None)
        for did in first['delivery_ids']:
            db['DELIVERY'].update_item(Key={'deliveryId': did}, UpdateExpression='SET acknowledgedAt = :now', ExpressionAttributeValues={':now':'2026-01-01'})
        event['delivery']['recipients'].reverse()
        second = module.handler(event, None)
        assert set(first['delivery_ids']) == set(second['delivery_ids'])
        assert (sender.send_email if channel == 'email' else sender.publish).call_count == 2
        assert all(row['acknowledgedAt'] for row in db['DELIVERY'].scan()['Items'])


def test_uncertain_send_is_not_blindly_repeated(db, signal):
    from src.delivery import email_sender
    from shared.delivery_state import DeliveryUncertain, reserve
    event = {'signal': signal.to_event(), 'delivery': {'persona_id': 'p', 'channel':'email', 'recipients':['a@example.test']}}
    with patch.object(email_sender, 'ses') as sender:
        sender.send_email.side_effect = RuntimeError('timeout')
        with pytest.raises(RuntimeError):
            email_sender.handler(event, None)
        sender.send_email.side_effect = None
        with pytest.raises(DeliveryUncertain):
            email_sender.handler(event, None)
        assert sender.send_email.call_count == 1
    table = MagicMock()
    table.put_item.side_effect = error()
    with pytest.raises(ClientError):
        reserve(table, 'd', event['signal'], event['delivery'], 'a')


def test_subscription_proxy_returns_real_result_and_propagates_failure(db):
    from src.api import management
    with patch.object(management, 'lambda_client') as client:
        client.invoke.return_value = {'Payload': io.BytesIO(b'{"statusCode":201,"body":"ok"}')}
        assert management.handler(api('/v1/personas/{personaId}/subscribe', 'POST', {'naturalLanguage':'RDS'}, {'personaId':'p'}), None)['statusCode'] == 201
        client.invoke.return_value = {'FunctionError': 'Unhandled'}
        with pytest.raises(RuntimeError):
            management.handler(api('/v1/personas/{personaId}/subscribe', 'POST'), None)


def test_learning_uses_distinct_incident_ratio_and_updates_one_rule(db):
    from src.learning import feedback_processor as learning
    from shared.runtime import iso
    db['PERSONA'].put_item(Item={'personaId':'p','orgId':'default'})
    for i in range(3):
        db['DELIVERY'].put_item(Item={'deliveryId':str(i),'personaId':'p','signalId':str(i),'signalSource':'app','feedback':'noise','deliveredAt':iso()})
    args = dict(delivery_table=db['DELIVERY'],persona_table=db['PERSONA'],persona_id='p',signal_id='0',new_image={})
    learning._check_noise_suppression(**args)
    learning._check_noise_suppression(**args)
    row = db['PERSONA'].get_item(Key={'personaId':'p'})['Item']
    assert len(row['learnedSuppressions']) == 1
    assert next(iter(row['learnedSuppressions'].values()))['confidence'] == 1
    # A second recipient of the same incident must not count as a second incident.
    db['DELIVERY'].put_item(Item={'deliveryId':'duplicate','personaId':'p','signalId':'0','signalSource':'app','feedback':'useful','deliveredAt':iso()})
    learning._check_noise_suppression(**args)
    assert db['PERSONA'].get_item(Key={'personaId':'p'})['Item']['learnedSuppressions'] == {}


def test_only_complete_suppression_is_recorded_as_reduction(db, signal):
    from src.persona import audience_router as router
    signal.content.structured_data = {}
    db['SIGNAL'].put_item(Item=signal.to_dynamo())
    rule = {'source':'manual','pattern':{'source':'test.app'}}
    db['PERSONA'].put_item(Item={'personaId':'persona-sre','suppressionRules':[rule]})
    db['PERSONA'].put_item(Item={'personaId':'custom','subscriptions':[{'filter':{'sources':['test.app']}}],'suppressionRules':[rule]})
    with patch.object(router,'dynamodb',boto3.resource('dynamodb')):
        result = router.handler({'signal':signal.to_event()},None)
        assert result['persona_ids'] == [] and result['signal']['status'] == 'suppressed'
        key = {'signalId':signal.signal_id,'ingestedAt':signal.ingested_at}
        assert db['SIGNAL'].get_item(Key=key)['Item']['status'] == 'suppressed'
        # A signal delivered to one eligible persona must not be counted as fully suppressed.
        db['SIGNAL'].put_item(Item=signal.to_dynamo())
        db['PERSONA'].put_item(Item={'personaId':'custom','subscriptions':[{'filter':{'sources':['test.app']}}]})
        assert router.handler({'signal':signal.to_event()},None)['persona_ids'] == ['custom']
        assert db['SIGNAL'].get_item(Key=key)['Item']['status'] == 'new'
