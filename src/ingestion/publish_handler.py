"""Publish API with authenticated organization binding and transactional outbox."""
import os
import boto3
from pydantic import ValidationError
from shared.models import SignalEvent
from shared.runtime import authorize, body, response
from shared.ingest import persist

kinesis = boto3.client('kinesis')
dynamodb = boto3.resource('dynamodb')


def handler(event, context):
    try:
        org = authorize(event)
        data = body(event)
        allowed = {'source', 'signal_type', 'severity', 'content', 'context', 'audience_hint', 'correlation'}
        if set(data) - allowed:
            raise ValueError('Unknown signal fields')
        signal = SignalEvent(**data, org_id=org)
        source_accounts = os.environ.get('SOURCE_ACCOUNT_IDS', '').split(',')
        if signal.context.account_id and signal.context.account_id not in source_accounts:
            raise PermissionError('Signal account is outside this organization deployment')
        headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
        sid = persist(signal, dynamodb.Table(os.environ['SIGNAL_TABLE_NAME']), headers.get('idempotency-key'))
        return response(201, {'signalId': sid, 'status': 'new'})
    except PermissionError as exc:
        return response(403, {'error': str(exc)})
    except (ValueError, KeyError, TypeError, ValidationError) as exc:
        return response(400, {'error': str(exc)})
