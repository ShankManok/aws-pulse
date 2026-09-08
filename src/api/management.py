"""IAM-authenticated management and read APIs for one isolated deployment tenant."""
import base64
import json
import os
import ulid
import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError
from shared.runtime import authorize, body, response, tenant, owns, dynamo, dumps, iso
from shared.personas import Persona


dynamodb = boto3.resource('dynamodb')
lambda_client = boto3.client('lambda')


def table(kind):
    return dynamodb.Table(os.environ[f'{kind}_TABLE_NAME'])


def handler(event, context):
    try:
        authorize(event)
        route = event.get('resource', '')
        method = event.get('httpMethod', 'GET')
        params = event.get('pathParameters') or {}
        if route == '/v1/personas' and method == 'POST':
            return create_persona(body(event))
        if route == '/v1/personas/{personaId}' and method == 'PUT':
            return update_persona(params['personaId'], body(event))
        if route == '/v1/personas/{personaId}/subscribe' and method == 'POST':
            result = lambda_client.invoke(FunctionName=os.environ['SUBSCRIPTION_FUNCTION_NAME'], InvocationType='RequestResponse', Payload=dumps(event).encode())
            if result.get('FunctionError'):
                raise RuntimeError('Subscription failed')
            return json.loads(result['Payload'].read())
        if route == '/v1/signals/{signalId}' and method == 'GET':
            return get_signal(params['signalId'])
        if route == '/v1/deliveries' and method == 'GET':
            return list_deliveries(event.get('queryStringParameters') or {})
        if route == '/v1/feedback' and method == 'POST':
            return feedback(body(event))
        if route == '/v1/analytics/nrs' and method == 'GET':
            return analytics(event.get('queryStringParameters') or {})
        return response(404, {'error': 'Unknown route'})
    except PermissionError as exc:
        return response(403, {'error': str(exc)})
    except (ValueError, KeyError, TypeError, ValidationError) as exc:
        return response(400, {'error': str(exc)})
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return response(409, {'error': 'Resource changed; reload and retry'})
        raise


def create_persona(data):
    org = data.pop('orgId', tenant())
    if org != tenant():
        raise PermissionError('Organization mismatch')
    parsed = Persona.model_validate(data).model_dump(exclude_none=True)
    pid = 'persona-' + str(ulid.new())
    table('PERSONA').put_item(Item=dynamo({**parsed, 'personaId': pid, 'orgId': tenant(), 'createdAt': iso(), 'version': 1,
        'subscriptions': [], 'suppressionRules': []}), ConditionExpression='attribute_not_exists(personaId)')
    return response(201, {'personaId': pid, 'version': 1})


def update_persona(pid, updates):
    current = table('PERSONA').get_item(Key={'personaId': pid}, ConsistentRead=True).get('Item')
    if not current or not owns(current):
        return response(404, {'error': 'Persona not found'})
    expected = updates.pop('version', current.get('version', 1))
    allowed = set(Persona.model_fields)
    if set(updates) - allowed:
        raise ValueError('Only persona configuration fields may be updated')
    config = {key: current[key] for key in allowed if key in current}
    config.update(updates)
    validated = Persona.model_validate(config).model_dump(exclude_none=True)
    item = {**current, **validated, 'version': int(expected) + 1, 'updatedAt': iso()}
    table('PERSONA').put_item(Item=dynamo(item), ConditionExpression='attribute_exists(personaId) AND (#v = :v OR attribute_not_exists(#v))',
        ExpressionAttributeNames={'#v': 'version'}, ExpressionAttributeValues={':v': expected})
    return response(200, {'personaId': pid, 'version': item['version']})


def get_signal(sid):
    result = table('SIGNAL').query(KeyConditionExpression='signalId = :id', ExpressionAttributeValues={':id': sid}, ConsistentRead=True, Limit=1)
    rows = result.get('Items', [])
    if not rows or rows[0].get('recordType') == 'receipt' or not owns(rows[0]):
        return response(404, {'error': 'Signal not found'})
    item = rows[0]
    item['signalType'] = item.get('signal_type', '')
    return response(200, item)


def list_deliveries(params):
    limit = int(params.get('limit', 50))
    if not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    values = {':org': tenant()}
    kwargs = {'Limit': limit, 'FilterExpression': 'orgId = :org', 'ExpressionAttributeValues': values}
    operation = 'scan'
    if params.get('signalId') or params.get('personaId'):
        field = 'signalId' if params.get('signalId') else 'personaId'
        operation = 'query'
        kwargs.update(IndexName='by-signal' if field == 'signalId' else 'by-persona', KeyConditionExpression=f'{field} = :id')
        values[':id'] = params[field]
    if params.get('signalId') and params.get('personaId'):
        kwargs['FilterExpression'] += ' AND personaId = :persona'
        values[':persona'] = params['personaId']
    if params.get('nextToken'):
        cursor = json.loads(base64.urlsafe_b64decode(params['nextToken']))
        if cursor['org'] != tenant() or cursor['filter'] != {k: v for k, v in params.items() if k != 'nextToken'}:
            raise ValueError('Pagination token does not match request')
        kwargs['ExclusiveStartKey'] = cursor['key']
    result = getattr(table('DELIVERY'), operation)(**kwargs)
    records = [{k: v for k, v in row.items() if k not in ('actionTokenHash', 'signal', 'escalationChain')} for row in result.get('Items', [])]
    out = {'deliveries': records}
    if result.get('LastEvaluatedKey'):
        out['nextToken'] = base64.urlsafe_b64encode(dumps({'org': tenant(), 'filter': {k: v for k, v in params.items() if k != 'nextToken'}, 'key': result['LastEvaluatedKey']}).encode()).decode()
    return response(200, out)


def feedback(data):
    did, feedback_value = data['deliveryId'], data['feedback']
    if feedback_value not in ('useful', 'noise', 'escalate', 'resolved'):
        raise ValueError('Invalid feedback')
    row = table('DELIVERY').get_item(Key={'deliveryId': did}, ConsistentRead=True).get('Item')
    if not row or not owns(row):
        return response(404, {'error': 'Delivery not found'})
    # IAM authorization is enforced above; mutation logic is shared with token callbacks.
    result = lambda_client.invoke(FunctionName=os.environ['ACTION_FUNCTION_NAME'], InvocationType='RequestResponse',
        Payload=dumps({'internalFeedback': {'deliveryId': did, 'feedback': feedback_value, 'orgId': tenant()}}).encode())
    if result.get('FunctionError'):
        raise RuntimeError('Feedback action failed')
    return json.loads(result['Payload'].read())


def analytics(params):
    if params.get('orgId', tenant()) != tenant():
        raise PermissionError('Organization mismatch')
    from shared.runtime import items
    rows = items(table('ANALYTICS'), 'query', IndexName='by-org-date', KeyConditionExpression='orgId = :org',
                 ExpressionAttributeValues={':org': tenant()})
    return response(200, {'orgId': tenant(), 'snapshots': sorted(rows, key=lambda row: row['date'])})
