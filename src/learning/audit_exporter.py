"""Append-only delivery history plus paginated hourly reconciliation snapshots."""
import hashlib
import os
from datetime import timedelta
import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from shared.runtime import now, iso, dumps, items, owns

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')


def _write(bucket, key, content):
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=content.encode(), ContentType='application/x-ndjson',
                      ServerSideEncryption='AES256', IfNoneMatch='*')
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'PreconditionFailed':
            raise


def handler(event, context):
    bucket = os.environ['AUDIT_BUCKET_NAME']
    if not bucket:
        raise ValueError('AUDIT_BUCKET_NAME is required')
    if 'Records' in event:
        failures = []
        for record in event['Records']:
            try:
                value = record['dynamodb'].get('NewImage') or record['dynamodb'].get('OldImage', {})
                row = {key: TypeDeserializer().deserialize(value) for key, value in value.items()}
                if not owns(row):
                    raise PermissionError('Organization mismatch')
                row.pop('actionTokenHash', None)
                content = dumps({'eventId': record['eventID'], 'operation': record['eventName'],
                                 'record': row, 'timestamp': record['dynamodb'].get('ApproximateCreationDateTime')})
                key = 'audit/events/' + hashlib.sha256(record['eventID'].encode()).hexdigest() + '.jsonl'
                _write(bucket, key, content)
            except Exception:
                failures.append({'itemIdentifier': record['dynamodb']['SequenceNumber']})
        return {'batchItemFailures': failures}
    end = now().replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=1)
    rows = _scan_deliveries(dynamodb.Table(os.environ['DELIVERY_TABLE_NAME']), iso(start), iso(end))
    if not rows:
        return {'statusCode': 200, 'exported': 0}
    content = '\n'.join(dumps({k: v for k, v in row.items() if k != 'actionTokenHash'}) for row in sorted(rows, key=lambda row: row['deliveryId']))
    key = f'audit/snapshots/date={start:%Y-%m-%d}/hour={start:%H}/' + hashlib.sha256(content.encode()).hexdigest() + '.jsonl'
    _write(bucket, key, content)
    return {'statusCode': 200, 'exported': len(rows), 's3_key': key}


def _scan_deliveries(table, window_start, window_end):
    return [row for row in items(table, FilterExpression='deliveredAt >= :start AND deliveredAt < :end',
             ExpressionAttributeValues={':start': window_start, ':end': window_end}) if owns(row)]
