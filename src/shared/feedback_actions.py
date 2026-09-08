"""Idempotent feedback actions shared by API and email/Chatbot confirmation."""
import os
from datetime import timedelta
import boto3
from shared.runtime import iso, now, dumps, tenant, owns, dynamo

lambda_client = boto3.client('lambda')
dynamodb = boto3.resource('dynamodb')


def follow_up(record, action):
    if action == 'noise':
        if record.get('signal', {}).get('severity', {}).get('level') in ('critical', 'high'):
            return  # Never create a rule hiding urgent signals from a feedback click.
        source = record.get('signalSource')
        if not source:
            raise ValueError('Delivery lacks a source for scoped suppression')
        table = dynamodb.Table(os.environ['PERSONA_TABLE_NAME'])
        persona = table.get_item(Key={'personaId': record['personaId']}, ConsistentRead=True).get('Item')
        if not persona or not owns(persona):
            raise PermissionError('Persona not in this organization')
        rule = {'id': 'manual-' + record['deliveryId'], 'source': 'manual',
                'pattern': {'source': source, 'signal_type': record.get('signalType', ''),
                            'severity': record.get('signal', {}).get('severity', {}).get('level', 'low')},
                'expiresAt': iso(now() + timedelta(days=1))}
        # Separate map avoids concurrent lost updates and repeated appended rules.
        table.update_item(Key={'personaId': record['personaId']},
            UpdateExpression='SET manualSuppressions = if_not_exists(manualSuppressions, :empty)',
            ExpressionAttributeValues={':empty': {}})
        table.update_item(Key={'personaId': record['personaId']}, UpdateExpression='SET manualSuppressions.#id = :rule',
            ExpressionAttributeNames={'#id': rule['id']}, ExpressionAttributeValues={':rule': dynamo(rule)})
    elif action == 'escalate':
        result = lambda_client.invoke(FunctionName=os.environ['ESCALATION_FUNCTION_NAME'], InvocationType='RequestResponse',
            Payload=dumps({'delivery_id': record['deliveryId'], 'signal': record['signal'], 'persona_id': record['personaId'],
                           'escalation_chain': record.get('escalationChain', []), 'manual': True, 'org_id': tenant()}).encode())
        import json
        payload = json.loads(result['Payload'].read()) if not result.get('FunctionError') else {}
        if result.get('FunctionError') or payload.get('statusCode', 500) >= 400:
            raise RuntimeError('Escalation failed; retry the action')
