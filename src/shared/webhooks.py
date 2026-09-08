"""Webhook decoding and Secrets Manager retrieval without logging secret values."""
import json
import os
import boto3
from shared.runtime import body


def secret(env_name):
    arn = os.environ.get('WEBHOOK_SECRET_ARN')
    if arn:
        result = boto3.client('secretsmanager').get_secret_value(SecretId=arn)
        return json.loads(result['SecretString']).get(env_name, '')
    return os.environ.get(env_name, '')


def decode(event):
    value = body(event)
    return value
