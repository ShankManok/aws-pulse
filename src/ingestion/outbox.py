"""DynamoDB Stream outbox: acknowledge a record only after Kinesis accepts it."""
import os
import boto3
from boto3.dynamodb.types import TypeDeserializer
from shared.runtime import dumps

kinesis = boto3.client('kinesis')
deserializer = TypeDeserializer()


def handler(event, context):
    failures = []
    for record in event.get('Records', []):
        try:
            if record['eventName'] != 'INSERT':
                continue
            signal = {k: deserializer.deserialize(v) for k, v in record['dynamodb']['NewImage'].items()}
            if signal.get('recordType') != 'signal':
                continue
            for key in ('signalId', 'ingestedAt', 'recordType'):
                signal.pop(key, None)
            kinesis.put_record(StreamName=os.environ['SIGNAL_STREAM_NAME'], Data=dumps(signal),
                               PartitionKey=signal.get('org_id', 'default') + ':' + signal.get('context', {}).get('account_id', signal['signal_id']))
        except Exception:
            failures.append({'itemIdentifier': record['dynamodb']['SequenceNumber']})
    return {'batchItemFailures': failures}
