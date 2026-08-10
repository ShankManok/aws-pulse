# Persona Configuration Guide

## Overview

Personas define who gets notified, how content is tailored, and through which channels. Each persona has a role template, language level, delivery preferences, subscriptions, and suppression rules.

## Default Personas (Seeded on Deploy)

| Persona ID | Name | Role | Language Level | Channels | Escalation SLA |
|------------|------|------|----------------|----------|----------------|
| persona-ciso | CISO | ciso | executive | email | 15 min |
| persona-sre | SRE | sre | detailed_technical | email, slack | 10 min |
| persona-cto | CTO | cto | technical_summary | email | 30 min |

## Persona Schema

```json
{
  "personaId": "persona-sre",
  "orgId": "default",
  "name": "SRE",
  "roleTemplate": "sre",
  "languageLevel": "detailed_technical",
  "members": [
    {"principalId": "sre-team@company.com", "channels": ["email", "slack"]}
  ],
  "deliveryPreferences": {
    "channels": ["email", "slack"],
    "cadence": "realtime",
    "escalationAfterMinutes": 10
  },
  "subscriptions": [],
  "suppressionRules": []
}
```

## Role Templates

| Template | Content Style |
|----------|--------------|
| `ciso` | Business risk, blast radius, compliance impact. Executive language, max 3 sentences. |
| `sre` | Resource IDs, error codes, metrics, timeline, remediation steps. Full technical detail. |
| `cto` | Incident patterns, team metrics, strategic implications. Executive summary. |
| `finops` | Cost impact, budget implications, optimization opportunities. Business data focus. |
| `compliance` | Regulatory mapping, audit trail, control effectiveness. Formal regulatory language. |

## Language Levels

| Level | Description |
|-------|-------------|
| `executive` | 2-3 sentences, business impact focus |
| `technical_summary` | 1 paragraph, key technical points |
| `detailed_technical` | Full detail with IDs, codes, steps |
| `business_data` | Numbers, costs, trends |
| `formal_regulatory` | Compliance framework references |

## Adding Members

Update a persona's members list via the DynamoDB console or SDK:

```python
from pulse import PulseClient

client = PulseClient(endpoint_url="...", region="ap-southeast-1")
client.update_persona("persona-sre", members=[
    {"principalId": "alice@company.com", "channels": ["email", "slack"]},
    {"principalId": "bob@company.com", "channels": ["email"]},
    {"principalId": "sre-oncall-channel", "channels": ["slack"]},
])
```

## Subscriptions (Natural Language)

Create custom notification rules using natural language:

```bash
curl -X POST "$PERSONA_API_URL/v1/personas/persona-sre/subscribe" \
  -H "Content-Type: application/json" \
  -d '{"naturalLanguage": "Notify me when any production RDS has a failover in ap-southeast-1"}'
```

The system uses Bedrock to parse this into a structured filter:
```json
{
  "sources": ["aws.rds"],
  "regions": ["ap-southeast-1"],
  "tags": {"Environment": "production"},
  "signal_types": ["incident"],
  "keywords": ["failover"]
}
```

Signals are matched against subscriptions using AND logic across fields.

## Suppression Rules

Suppression rules prevent noise by auto-skipping signals that match certain patterns.

**Learned rules** are created automatically when a persona marks 3+ signals from the same source as "noise" within 30 days.

**Manual rules** can be added directly:
```json
{
  "id": "manual-suppress-info",
  "source": "manual",
  "pattern": {"severity": "informational"},
  "confidence": 1.0
}
```

## Routing Logic

Signals are routed to personas through four strategies (in order):

1. **Audience hints** — Signal's `audience_hint.personas` field maps directly
2. **Severity routing** — Critical/High → all personas; Medium → SRE+CTO; Low → SRE only
3. **Signal type** — Findings → CISO; Recommendations → CTO
4. **Subscription matching** — Custom NL subscriptions (AND logic across filter fields)

After routing, **suppression rules** filter out noise before delivery.

## Escalation

When `escalationAfterMinutes` is configured and a notification isn't acknowledged within the SLA, Pulse auto-escalates to the next persona in the `escalation_chain`.

The escalation chain is defined per-signal in `audience_hint.escalation_chain`:
```json
"escalation_chain": ["persona-sre", "persona-cto"]
```
