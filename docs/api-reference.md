# API Reference

## Base URLs

| API | URL Pattern |
|-----|-------------|
| Publish API | `https://<id>.execute-api.<region>.amazonaws.com/<stage>/` |
| Callback API | `https://<id>.execute-api.<region>.amazonaws.com/<stage>/` |
| Persona API | `https://<id>.execute-api.<region>.amazonaws.com/<stage>/` |

Get URLs from CDK outputs after deployment.

## Authentication

- **Publish API**: Requires `x-api-key` header (API Gateway API Key)
- **Webhook endpoints**: Provider-specific auth (see docs/webhooks.md)
- **SDK clients**: SigV4 signing via AWS credentials

---

## POST /v1/signals

Publish a signal to AWS Pulse.

**Headers:**
```
Content-Type: application/json
x-api-key: <your-api-key>
```

**Request Body:**
```json
{
  "source": "custom.myapp",
  "signal_type": "incident",
  "severity": {
    "level": "high",
    "score": 75,
    "blast_radius": {
      "accounts": ["123456789012"],
      "resources": ["arn:aws:ec2:ap-southeast-1:123456789012:instance/i-abc"],
      "services": ["ec2"]
    }
  },
  "content": {
    "title": "EC2 instance unreachable",
    "raw_detail": "Instance i-abc failed health checks for 5 minutes",
    "recommended_actions": [
      {"action": "reboot", "label": "Reboot Instance", "api_call": "ec2:RebootInstances"}
    ]
  },
  "context": {
    "account_id": "123456789012",
    "region": "ap-southeast-1",
    "resource_arns": ["arn:aws:ec2:ap-southeast-1:123456789012:instance/i-abc"],
    "tags": {"Environment": "production"}
  },
  "audience_hint": {
    "personas": ["sre", "cto"],
    "escalation_chain": ["persona-sre", "persona-cto"],
    "sla_acknowledge_minutes": 10
  },
  "correlation": {
    "time_window_seconds": 300
  }
}
```

**Response (201):**
```json
{
  "signalId": "01HXYABC123...",
  "status": "new"
}
```

**Errors:**
- `400` — Missing required field
- `429` — Rate limited (exceeds 1000 req/sec)
- `500` — Internal error

**curl example:**
```bash
curl -X POST "$API_URL/v1/signals" \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source":"custom.test","signal_type":"incident","severity":{"level":"high","score":75},"content":{"title":"Test alert"}}'
```

---

## POST /v1/webhooks/{provider}

Ingest signals from third-party providers.

**Providers:** `pagerduty`, `datadog`, `servicenow`

See [docs/webhooks.md](webhooks.md) for provider-specific details.

---

## POST /v1/personas/{personaId}/subscribe

Create a natural language subscription for a persona.

**Request Body:**
```json
{
  "naturalLanguage": "Notify me when any production RDS fails over in ap-southeast-1"
}
```

**Response (201):**
```json
{
  "subscriptionId": "01HXYDEF456...",
  "naturalLanguage": "Notify me when any production RDS fails over in ap-southeast-1",
  "filter": {
    "sources": ["aws.rds"],
    "regions": ["ap-southeast-1"],
    "tags": {"Environment": "production"},
    "signal_types": ["incident"]
  }
}
```

---

## POST /v1/actions/{deliveryId}/{action}

Record an action on a delivered notification (from email buttons or API).

**Actions:** `acknowledge`, `escalate`, `suppress`

**Response (200):**
```json
{
  "deliveryId": "del-01HXY...",
  "action": "acknowledge",
  "recordedAt": "2026-08-10T12:00:00Z"
}
```

**GET** version returns HTML confirmation page (for email link clicks).

---

## Rate Limits

| Endpoint | Limit |
|----------|-------|
| POST /v1/signals | 1000 req/sec (2000 burst) |
| POST /v1/webhooks/* | No API key required, per-provider auth |
| POST /v1/personas/*/subscribe | Standard API limits |

---

## SDK Usage

```python
from pulse import PulseClient

client = PulseClient(
    endpoint_url="https://<api-id>.execute-api.ap-southeast-1.amazonaws.com/dev",
    region="ap-southeast-1",
)

response = client.publish_signal(
    source="custom.myapp",
    signal_type="incident",
    severity={"level": "high", "score": 75},
    content={"title": "Something broke"},
)
print(response.signal_id)
```

See `sdk/python/README.md` for full SDK documentation.
