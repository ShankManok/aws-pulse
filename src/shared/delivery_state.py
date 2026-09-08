"""Durable send claims: replay completed deliveries; never blindly repeat uncertain sends."""
import hashlib
import time
from botocore.exceptions import ClientError
from shared.action_tokens import new_token
from shared.runtime import dynamo, tenant, iso


class DeliveryUncertain(RuntimeError):
    """A prior send may have reached its destination; reconcile before replaying."""


def reserve(table, delivery_id, signal, delivery, recipient):
    token, digest = new_token()
    row = {'deliveryId': delivery_id, 'signalId': signal['signal_id'], 'personaId': delivery['persona_id'],
           'recipientId': recipient, 'channel': delivery['channel'], 'orgId': tenant(),
           'signalSource': signal.get('source', ''), 'signalType': signal.get('signal_type', ''),
           'signal': signal, 'escalationChain': delivery.get('escalation_chain', []),
           'escalationMinutes': delivery.get('escalation_after_minutes', 30),
           'actionTokenHash': digest, 'actionTokenExpiresAt': int(time.time()) + 86400,
           'contentVersion': hashlib.sha256(delivery.get('transformed_content', '').encode()).hexdigest(),
           'sendStatus': 'sending', 'createdAt': iso(), 'escalated': False}
    try:
        table.put_item(Item=dynamo(row), ConditionExpression='attribute_not_exists(deliveryId)')
        return token
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        previous = table.get_item(Key={'deliveryId': delivery_id}, ConsistentRead=True).get('Item', {})
        if previous.get('deliveredAt') or previous.get('sendStatus') == 'sent':
            return None
        raise DeliveryUncertain(f'Delivery {delivery_id} requires reconciliation; no duplicate was sent') from exc


def complete(table, delivery_id):
    table.update_item(Key={'deliveryId': delivery_id},
        UpdateExpression='SET sendStatus = :sent, deliveredAt = :now',
        ConditionExpression='sendStatus = :sending',
        ExpressionAttributeValues={':sent': 'sent', ':sending': 'sending', ':now': iso()})
