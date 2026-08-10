# Webhook Integration Guide

## Overview

Pulse accepts webhooks from PagerDuty, Datadog, and ServiceNow. Each webhook is
normalized into the canonical SignalEvent schema and processed through the same
intelligence pipeline as native AWS signals.

**Endpoint:** `POST /v1/webhooks/{provider}`

Providers: `pagerduty`, `datadog`, `servicenow`

---

## PagerDuty

### Setup in PagerDuty

1. Go to PagerDuty > Integrations > Generic Webhooks (v3)
2. Set webhook URL: `https://<api-url>/v1/webhooks/pagerduty`
3. Select events: `incident.triggered`, `incident.acknowledged`, `incident.resolved`
4. Copy the webhook signing secret

### Authentication

PagerDuty uses HMAC-SHA256 signature in the `X-PagerDuty-Signature` header.

Format: `v1=<hex-encoded-hmac-sha256>`

### Configure Secret

```bash
# Set in Lambda environment (via Parameter Store in production)
aws ssm put-parameter --name /pulse/dev/pagerduty-secret \
  --value "your-webhook-signing-secret" --type SecureString
```

### Severity Mapping

| PagerDuty Urgency | Pulse Severity | Score |
|-------------------|---------------|-------|
| high | high | 75 |
| low | low | 30 |

---

## Datadog

### Setup in Datadog

1. Go to Datadog > Integrations > Webhooks
2. Create a new webhook with URL: `https://<api-url>/v1/webhooks/datadog`
3. Add header: `DD-API-KEY: <your-pulse-datadog-key>`
4. Set payload template to include: title, priority, tags, body, alert_type

### Authentication

Datadog uses a custom `DD-API-KEY` header. This is a shared secret you configure
in both Datadog and the Pulse Lambda environment.

### Configure Key

```bash
aws ssm put-parameter --name /pulse/dev/datadog-api-key \
  --value "your-shared-api-key" --type SecureString
```

### Severity Mapping

| Datadog Priority | Pulse Severity | Score |
|-----------------|---------------|-------|
| P1 | critical | 95 |
| P2 | high | 75 |
| P3 | medium | 50 |
| P4 | low | 30 |
| P5 | informational | 10 |

### Tag Parsing

Datadog tags in `key:value` format are parsed into signal context:
- `region:us-east-1` → `context.region`
- `aws_account:123456789012` → `context.account_id`
- Tags with ARN values → `context.resource_arns`

---

## ServiceNow

### Setup in ServiceNow

1. Create a Business Rule or Flow that triggers on incident creation/update
2. Configure outbound REST message to: `https://<api-url>/v1/webhooks/servicenow`
3. Set Basic Auth credentials (username + password)
4. Send JSON body with: number, short_description, impact, urgency, state, category

### Authentication

ServiceNow uses HTTP Basic Authentication. The `Authorization` header contains
`Basic <base64(username:password)>`.

### Configure Credentials

```bash
aws ssm put-parameter --name /pulse/dev/servicenow-user --value "pulse_webhook" --type SecureString
aws ssm put-parameter --name /pulse/dev/servicenow-pass --value "secure-password" --type SecureString
```

### Severity Mapping (Impact × Urgency Matrix)

| Impact\Urgency | High (1) | Medium (2) | Low (3) |
|---------------|----------|-----------|---------|
| High (1) | critical (95) | high (80) | medium (60) |
| Medium (2) | high (75) | medium (50) | low (35) |
| Low (3) | medium (55) | low (30) | informational (10) |

### Category Routing

| ServiceNow Category | Signal Type | Persona |
|--------------------|-------------|---------|
| Security | finding | CISO |
| Change/Request | lifecycle | SRE |
| Other | incident | SRE |

---

## Testing Webhooks

### PagerDuty (curl)

```bash
BODY='{"event":{"event_type":"incident.triggered","data":{"id":"PD1","title":"Test","urgency":"high","service":{"summary":"Web"}}}}'
SIG="v1=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "your-secret" | cut -d' ' -f2)"

curl -X POST "$API_URL/v1/webhooks/pagerduty" \
  -H "X-PagerDuty-Signature: $SIG" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

### Datadog (curl)

```bash
curl -X POST "$API_URL/v1/webhooks/datadog" \
  -H "DD-API-KEY: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"title":"CPU Alert","priority":"P2","alert_type":"metric_alert","tags":["env:prod"],"body":"CPU > 90%"}'
```

### ServiceNow (curl)

```bash
curl -X POST "$API_URL/v1/webhooks/servicenow" \
  -H "Authorization: Basic $(echo -n 'user:pass' | base64)" \
  -H "Content-Type: application/json" \
  -d '{"number":"INC001","short_description":"DB timeout","impact":1,"urgency":1,"state":"New","category":"Software"}'
```
