# AWS Pulse Python SDK

Python client for the AWS Pulse intelligent notification API. Authenticates with SigV4 signing using your AWS credentials.

## Installation

```bash
pip install aws-pulse-sdk
```

Or from source:

```bash
cd sdk/python
pip install -e .
```

## Quick Start

```python
from pulse import PulseClient

# Initialize client (uses default AWS credentials from environment/profile)
client = PulseClient(
    endpoint_url="https://abc123.execute-api.ap-southeast-1.amazonaws.com/dev",
    region="ap-southeast-1",
)

# Publish a signal
response = client.publish_signal(
    source="custom.myapp",
    signal_type="incident",
    severity={
        "level": "high",
        "score": 75,
        "blast_radius": {
            "accounts": ["123456789012"],
            "resources": ["arn:aws:ec2:ap-southeast-1:123456789012:instance/i-abc123"],
            "services": ["ec2"],
        },
    },
    content={
        "title": "EC2 instance unreachable",
        "raw_detail": "Instance i-abc123 failed health checks for 5 minutes",
        "recommended_actions": [
            {"action": "reboot", "label": "Reboot Instance", "api_call": "ec2:RebootInstances"}
        ],
    },
    context={
        "account_id": "123456789012",
        "region": "ap-southeast-1",
        "resource_arns": ["arn:aws:ec2:ap-southeast-1:123456789012:instance/i-abc123"],
        "tags": {"Environment": "production", "Team": "platform"},
    },
    audience_hint={
        "personas": ["sre", "cto"],
        "escalation_chain": ["persona-sre", "persona-cto"],
        "sla_acknowledge_minutes": 10,
    },
)

print(f"Signal published: {response.signal_id}")
```

## API Reference

### `PulseClient(endpoint_url, region, max_retries=3, timeout=30)`

Create a client instance.

| Parameter | Type | Description |
|-----------|------|-------------|
| `endpoint_url` | str | Base URL of the Pulse API |
| `region` | str | AWS region (default: "ap-southeast-1") |
| `max_retries` | int | Max retries on transient failures (default: 3) |
| `timeout` | int | HTTP timeout in seconds (default: 30) |

### `client.publish_signal(...) -> PublishSignalResponse`

Publish an operational signal to Pulse for processing and delivery.

### `client.get_signal(signal_id) -> SignalResponse`

Retrieve a signal by its unique ID.

### `client.create_persona(...) -> PersonaResponse`

Create a new persona with delivery preferences.

```python
response = client.create_persona(
    name="SRE On-Call",
    role_template="sre",
    members=[
        {"principalId": "oncall@company.com", "channels": ["email", "slack"]},
    ],
    language_level="detailed_technical",
    delivery_preferences={
        "channels": ["email", "slack"],
        "cadence": "realtime",
        "escalationAfterMinutes": 10,
    },
)
```

### `client.update_persona(persona_id, **updates) -> PersonaResponse`

Update persona configuration.

```python
client.update_persona(
    "persona-sre",
    deliveryPreferences={"channels": ["email", "slack"], "cadence": "realtime", "escalationAfterMinutes": 5},
)
```

### `client.list_deliveries(signal_id=None, persona_id=None, limit=50) -> ListDeliveriesResponse`

List delivery records with optional filtering.

```python
deliveries = client.list_deliveries(signal_id="01HXY...", limit=10)
for d in deliveries.deliveries:
    print(f"{d.delivery_id}: {d.channel} -> {d.recipient_id} ({d.delivered_at})")
```

### `client.submit_feedback(delivery_id, feedback) -> dict`

Submit feedback on a notification. Feedback values: `"useful"`, `"noise"`, `"escalate"`.

## Error Handling

```python
from pulse import PulseClient, PulseValidationError, PulseNotFoundError, PulseThrottlingError

client = PulseClient(endpoint_url="...", region="ap-southeast-1")

try:
    signal = client.get_signal("nonexistent-id")
except PulseNotFoundError:
    print("Signal not found")
except PulseThrottlingError:
    print("Rate limited, try again later")
except PulseValidationError as e:
    print(f"Bad request: {e}")
```

## Authentication

The SDK uses AWS SigV4 signing with credentials from the standard credential chain:

1. Environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
2. Shared credentials file (`~/.aws/credentials`)
3. IAM role (for Lambda, EC2, ECS, etc.)

The caller needs `execute-api:Invoke` permission on the Pulse API Gateway.

## Requirements

- Python >= 3.10
- boto3 >= 1.34.0
- requests >= 2.31.0
- pydantic >= 2.5.0
