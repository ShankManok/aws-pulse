"""Random delivery-scoped capabilities. Only a hash is stored in DynamoDB."""
import hashlib
import secrets
from urllib.parse import urlencode


def new_token():
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def action_url(base, delivery_id, action, token):
    return f"{base.rstrip('/')}/v1/actions/{delivery_id}/{action}?{urlencode({'token': token})}"
