"""Transactional ingestion receipt and outbox; stream publication is asynchronous."""
import hashlib
import time
import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from shared.runtime import dumps, tenant

serializer = TypeSerializer()


def persist(signal, table, idempotency_key=None):
    signal.org_id = tenant()
    item = signal.to_dynamo()
    item['recordType'] = 'signal'
    if not idempotency_key:
        table.put_item(Item=item, ConditionExpression='attribute_not_exists(signalId)')
        return signal.signal_id
    if len(idempotency_key) > 256:
        raise ValueError('Idempotency key must be at most 256 characters')
    receipt_key = 'receipt-' + hashlib.sha256(f'{tenant()}:{idempotency_key}'.encode()).hexdigest()
    content = signal.to_event()
    for name in ('signal_id', 'ingested_at', 'status'):
        content.pop(name, None)
    fingerprint = hashlib.sha256(dumps(content).encode()).hexdigest()
    receipt = {'signalId': receipt_key, 'ingestedAt': 'RECEIPT', 'recordType': 'receipt',
               'targetId': signal.signal_id, 'requestHash': fingerprint, 'orgId': tenant(), 'ttl': int(time.time()) + 30 * 86400}
    try:
        boto3.client('dynamodb').transact_write_items(TransactItems=[
            {'Put': {'TableName': table.name, 'Item': {k: serializer.serialize(v) for k, v in row.items()},
                     'ConditionExpression': 'attribute_not_exists(signalId)'}} for row in (receipt, item)])
        return signal.signal_id
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'TransactionCanceledException':
            raise
        existing = table.get_item(Key={'signalId': receipt_key, 'ingestedAt': 'RECEIPT'}, ConsistentRead=True).get('Item')
        if not existing:
            raise
        if existing['requestHash'] != fingerprint:
            raise ValueError('Idempotency key was already used with a different payload') from exc
        return existing['targetId']
