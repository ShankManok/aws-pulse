"""Shared serialization, pagination, time and deployment tenant boundaries."""
import base64
import json
import os
from datetime import datetime, timezone
from decimal import Decimal


def now():
    return datetime.now(timezone.utc)


def iso(value=None):
    return (value or now()).isoformat().replace('+00:00', 'Z')


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def json_value(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral() else float(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f'Unsupported JSON value: {type(value).__name__}')


def dumps(value):
    return json.dumps(value, default=json_value, sort_keys=True, allow_nan=False)


def dynamo(value):
    return json.loads(dumps(value), parse_float=Decimal)


def pages(table, operation='scan', **kwargs):
    while True:
        response = getattr(table, operation)(**kwargs)
        yield response
        key = response.get('LastEvaluatedKey')
        if not key:
            return
        kwargs['ExclusiveStartKey'] = key


def items(table, operation='scan', **kwargs):
    return [item for page in pages(table, operation, **kwargs) for item in page.get('Items', [])]


def tenant():
    return os.environ.get('ORG_ID', 'default')


def owns(record):
    return record.get('orgId', record.get('org_id', 'default')) == tenant()


def authorize(event):
    account = (event.get('requestContext', {}).get('identity') or {}).get('accountId', '')
    allowed = os.environ.get('API_ACCOUNT_IDS', '').split(',')
    if not account or account not in allowed:
        raise PermissionError('Caller is not authorized for this organization')
    return tenant()


def body(event):
    raw = event.get('body') or '{}'
    if event.get('isBase64Encoded'):
        raw = base64.b64decode(raw, validate=True).decode('utf-8')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def response(status, value):
    return {'statusCode': status, 'headers': {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}, 'body': dumps(value)}
