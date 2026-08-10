# AWS Pulse Architecture

## Overview

AWS Pulse is an intelligent notification infrastructure that sits between AWS services (signal producers) and human consumers (personas). It ingests signals from multiple sources, correlates them, transforms content per-persona using AI, and delivers through the right channel at the right time.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              Signal Producers                                     │
├────────────┬────────────┬────────────┬────────────┬────────────┬────────────────┤
│ CloudWatch │ SecurityHub│ AWS Health │ GuardDuty  │ PagerDuty  │ Datadog/SNOW   │
└─────┬──────┴─────┬──────┴─────┬──────┴─────┬──────┴─────┬──────┴───────┬────────┘
      │            │            │            │            │              │
      ▼            ▼            ▼            ▼            ▼              ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         INGESTION LAYER                                           │
│  ┌──────────────┐  ┌────────────────┐  ┌──────────────────────────────────┐     │
│  │ Publish API  │  │ EventBridge    │  │ Webhook Adapters                 │     │
│  │ POST /signals│  │ Rules          │  │ PagerDuty / Datadog / ServiceNow │     │
│  └──────┬───────┘  └───────┬────────┘  └──────────────┬───────────────────┘     │
│         │                  │                           │                          │
│         ▼                  ▼                           ▼                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐     │
│  │              Kinesis Data Stream (pulse-signals-{stage})                 │     │
│  └──────────────────────────────────┬──────────────────────────────────────┘     │
│                                     │                                            │
│  ┌──────────────────────────────────┼──────────────────────────────────────┐     │
│  │       DynamoDB (pulse-events-{stage}) — Signal persistence              │     │
│  └─────────────────────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────┬────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         INTELLIGENCE LAYER                                        │
│  ┌──────────────────────┐  ┌───────────────────────┐  ┌─────────────────────┐   │
│  │ Correlator Lambda    │  │ Predictors            │  │ CloudWatch Alarms   │   │
│  │ (Kinesis consumer)   │  │ (6h scheduled)        │  │ (errors/lag/SFN)    │   │
│  │ - Time-window group  │  │ - Linear extrapolation│  └─────────────────────┘   │
│  │ - Deduplication      │  │ - Trend detection     │                             │
│  └──────────┬───────────┘  └───────────────────────┘                             │
│             │                                                                     │
│  ┌──────────┴───────────────────────────────────────────────────────────────┐    │
│  │       DynamoDB (pulse-correlations-{stage}) — Correlation groups          │    │
│  └──────────────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────┬────────────────────────────────────────────┘
                                      │ Start Execution
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          PERSONA ENGINE                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐     │
│  │            Step Functions Workflow (pulse-persona-workflow)               │     │
│  │                                                                          │     │
│  │  ┌──────────┐    ┌─────────────┐    ┌──────────────────────────────┐    │     │
│  │  │ Audience │───▶│  Content    │───▶│  Map [per persona×channel]   │    │     │
│  │  │ Router   │    │ Transformer │    │  ├─ Choice(channel)          │    │     │
│  │  │          │    │ (Bedrock)   │    │  │  ├─ email → Email Sender  │    │     │
│  │  └──────────┘    └─────────────┘    │  │  └─ slack → Slack Sender  │    │     │
│  │                                      │  └─ Schedule Escalation      │    │     │
│  │                                      └──────────────────────────────┘    │     │
│  └─────────────────────────────────────────────────────────────────────────┘     │
│                                                                                   │
│  ┌──────────────────────┐  ┌───────────────────────────────────────────────┐     │
│  │ Subscription Agent   │  │ DynamoDB (pulse-personas-{stage})             │     │
│  │ NL → structured      │  │ - Persona configs, subscriptions, suppression │     │
│  │ filters (Bedrock)    │  └───────────────────────────────────────────────┘     │
│  └──────────────────────┘                                                         │
└─────────────────────────────────────┬────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          DELIVERY LAYER                                           │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────────────────────┐    │
│  │ SES Email    │  │ Slack via    │  │ Action Callback API                 │    │
│  │ (HTML)       │  │ SNS/Chatbot  │  │ POST /v1/actions/{id}/{action}      │    │
│  └──────┬───────┘  └──────┬───────┘  └──────────────────┬──────────────────┘    │
│         │                  │                              │                       │
│  ┌──────┴──────────────────┴──────────────────────────────┴──────────────────┐   │
│  │       DynamoDB (pulse-delivery-{stage}) — Delivery audit records           │   │
│  └───────────────────────────────────────────────────────────────────────────┘   │
│                                                                                   │
│  ┌────────────────────────────────────────────────────────────────────────────┐  │
│  │ Escalation Engine: EventBridge Scheduler → check ACK → re-invoke workflow  │  │
│  └────────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────┬────────────────────────────────────────────┘
                                      │ DDB Streams
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                       LEARNING & ANALYTICS LAYER                                  │
│  ┌──────────────────┐  ┌───────────────────┐  ┌───────────────────────────┐     │
│  │ Feedback         │  │ NRS Calculator    │  │ Suppression Recalculator │     │
│  │ Processor        │  │ (daily 02:30)     │  │ (daily 02:00)            │     │
│  │ (DDB Streams)    │  │ → CloudWatch      │  │ → prune expired rules    │     │
│  └──────────────────┘  └───────────────────┘  └───────────────────────────┘     │
│                                                                                   │
│  ┌──────────────────┐  ┌───────────────────┐  ┌───────────────────────────┐     │
│  │ Audit Exporter   │  │ S3 Audit Bucket   │  │ Athena Workgroup         │     │
│  │ (hourly)         │──▶│ (JSON Lines)      │◀──│ (compliance queries)     │     │
│  └──────────────────┘  └───────────────────┘  └───────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## CDK Stack Dependency Graph

```
IngestionStack (API, Kinesis, DDB, Webhooks, Rate Limiting)
DeliveryStack (DDB + DDB Streams, Action Callback API)
      ↑
PersonaStack (Personas DDB, Step Functions, Escalation, Subscription API)
      ↑
IntelligenceStack (Correlator, Predictor, DLQ, Alarms)
      ↑ (also depends on IngestionStack)
AnalyticsStack (Analytics DDB, S3 Audit, Athena)
      ↑ (depends on DeliveryStack)
LearningStack (Feedback Processor, NRS, Suppression)
      ↑ (depends on Delivery, Persona, Ingestion, Analytics)

OrgSetupStack (optional, cross-account — deployed separately)
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Kinesis over SQS | Ordered processing for time-window correlation |
| Step Functions over Lambda chaining | Visibility, retry logic, human-readable workflow |
| DynamoDB over Aurora | Predictable latency at scale, per-org isolation, TTL |
| Bedrock over custom models | No training data needed, rapid prompt iteration |
| EventBridge Scheduler over SFN Wait | Durable escalation timers survive workflow completion |
| Rule-based suppression (Phase 3) | Fast, explainable; ML model planned for Phase 5 |
| Linear extrapolation for predictions | Simple, no dependencies; anomaly detection in future |

## Data Models

See `.kiro/spec.md` for full TypeScript interface definitions:
- **SignalEvent** — canonical signal schema (DynamoDB)
- **Persona** — role config, members, delivery prefs, subscriptions, suppression rules
- **DeliveryRecord** — audit trail of every notification sent
- **CorrelationGroup** — grouped signals within time window

## Security

- API Gateway with API Key + Usage Plan rate limiting (1000 req/sec)
- SigV4 authentication for SDK clients
- Webhook signature validation per provider (HMAC-SHA256, API Key, Basic Auth)
- S3 audit bucket: AES-256 encryption, block all public access
- IAM least privilege per Lambda function
- Cross-account access via Organizations principal condition
