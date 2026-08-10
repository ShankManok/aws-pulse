# AWS Pulse - Kiro Engineering Spec

## Product Overview

AWS Pulse is an intelligent notification infrastructure that sits between AWS services (signal producers) and human consumers (personas). It ingests signals from multiple AWS services, correlates them, transforms content per-persona using AI, and delivers through the right channel at the right time.

## Architecture

```
Signal Producers → Ingestion Layer → Intelligence Layer → Persona Engine → Delivery Layer
                                          ↑                                      |
                                          └──── Feedback & Learning ←────────────┘

```

**Stack:** AWS CDK (TypeScript), Lambda (Python), DynamoDB, EventBridge, Bedrock, Step Functions, SES, API Gateway

---

## Requirements

### Functional Requirements

1. **Signal Ingestion**- Accept signals via REST API (Publish API) and EventBridge events
- Normalize signals from heterogeneous sources into a canonical SignalEvent schema
- Support AWS-native sources (CloudWatch, Security Hub, Health, GuardDuty) and third-party webhooks
- Buffer incoming signals in Kinesis for ordered, replayable processing
2. **Cross-Service Correlation**- Group signals that share affected resource ARNs within a configurable time window (default 5 min)
- Merge correlated signals into a single CorrelationGroup with a causality assessment
- Deduplicate identical/overlapping signals using content signature hashing
3. **AI Intelligence**- Score severity (0-100) using Bedrock, considering blast radius, business impact, and historical patterns
- Enrich signals with resource metadata (Resource Explorer), related documentation (Bedrock KB/RAG), and runbook links
- Generate predictive signals from CloudWatch metric trend analysis
4. **Persona Engine**- Store persona configurations: role template, language level, delivery preferences, suppression rules, subscriptions
- Transform signal content into persona-appropriate language using Bedrock (Claude/Nova)
- Route signals to matching personas based on resource tags, account ownership, and audience hints
- Support natural language subscription creation ("Notify me when...") via Bedrock Agents
5. **Smart Delivery**- Deliver via email (SES), Slack/Teams (Chatbot), Console (User Notifications), Mobile Push
- Embed actionable buttons (Acknowledge, Escalate, Suppress, Remediate) in notifications
- Implement escalation chains: auto-escalate if no ACK within configurable SLA
- Support cadence control: real-time, hourly batch, daily digest, weekly rollup per persona
6. **Feedback & Adaptive Learning**- Collect feedback (useful/noise/escalate) on every delivered notification
- Build per-persona suppression models from behavioral patterns
- Track and expose Notification Reduction Score (NRS), MTTA, MTTR metrics
7. **Compliance & Analytics**- Maintain immutable audit trail: who was notified, when, what content, whether acknowledged
- Expose analytics dashboard with NRS, fatigue scores, volume trends
- Generate monthly executive summary reports via Bedrock

### Non-Functional Requirements

- P1/P2 signals delivered in < 3 seconds (template-based generation)
- P3/P4 signals delivered in < 15 seconds (full Bedrock generation)
- Multi-tenant with per-org data isolation (DynamoDB per org, S3 per org)
- Cross-account support via AWS Organizations delegated admin model
- 99.9% availability for ingestion and delivery paths
- Horizontal scaling to 100K+ signals/day per tenant

---

## Data Models

### SignalEvent (DynamoDB)

```typescript
interface SignalEvent {
  signalId: string;              // ULID
  source: string;                // e.g., "aws.devops-agent", "aws.security-hub", "custom.myapp"
  signalType: 'incident' | 'finding' | 'recommendation' | 'prediction' | 'lifecycle';
  severity: {
    level: 'critical' | 'high' | 'medium' | 'low' | 'informational';
    score: number;               // 0-100, AI-assessed
    blastRadius: {
      accounts: string[];
      resources: string[];
      services: string[];
    };
  };
  content: {
    title: string;
    rawDetail: string;
    structuredData: Record<string, any>;
    recommendedActions: Array<{
      action: string;
      label: string;
      apiCall: string;
    }>;
  };
  context: {
    accountId: string;
    region: string;
    resourceArns: string[];
    tags: Record<string, string>;
    investigationId?: string;
    runbookUrl?: string;
  };
  audienceHint: {
    personas: string[];
    escalationChain: string[];
    slaAcknowledgeMinutes: number;
  };
  correlation: {
    correlationId?: string;
    timeWindowSeconds: number;
  };
  status: 'new' | 'correlated' | 'delivered' | 'acknowledged' | 'suppressed';
  ingestedAt: string;            // ISO timestamp
  correlationGroupId?: string;
}

```

### Persona (DynamoDB)

```typescript
interface Persona {
  personaId: string;
  orgId: string;
  name: string;
  roleTemplate: 'ciso' | 'soc_analyst' | 'cloud_ops' | 'sre' | 'finops' | 'compliance' | 'cto' | 'account_owner';
  members: Array<{
    principalId: string;         // IAM ARN or email
    channels: string[];          // ['email', 'slack', 'mobile']
  }>;
  languageLevel: 'executive' | 'technical_summary' | 'detailed_technical' | 'business_data' | 'formal_regulatory';
  deliveryPreferences: {
    channels: ('email' | 'slack' | 'teams' | 'console' | 'mobile' | 'sms')[];
    cadence: 'realtime' | 'hourly' | 'daily' | 'weekly';
    quietHours?: { start: string; end: string; timezone: string };
    escalationAfterMinutes: number;
  };
  subscriptions: Array<{
    id: string;
    naturalLanguage: string;     // Original NL rule
    eventBridgePattern: object;  // Compiled pattern
  }>;
  suppressionRules: Array<{
    id: string;
    source: 'manual' | 'learned';
    pattern: object;
    confidence: number;
  }>;
}

```

### DeliveryRecord (DynamoDB - Audit Trail)

```typescript
interface DeliveryRecord {
  deliveryId: string;
  signalId: string;
  personaId: string;
  recipientId: string;
  channel: string;
  contentVersion: string;        // Hash of persona-transformed content
  deliveredAt: string;
  acknowledgedAt?: string;
  escalated: boolean;
  feedback?: 'useful' | 'noise' | 'escalate';
}

```

### CorrelationGroup (DynamoDB)

```typescript
interface CorrelationGroup {
  groupId: string;
  signals: string[];             // signal IDs
  rootSignalId: string;
  timeWindow: { start: string; end: string };
  servicesAffected: string[];
  accountsAffected: string[];
  unifiedSeverity: SignalEvent['severity'];
  causalityGraph: object;        // DAG of signal relationships
  status: 'active' | 'resolved' | 'suppressed';
}

```

---

## API Endpoints

### POST /v1/signals (Publish API)

- **Auth:** SigV4 (AWS services) or API Key (3P)
- **Body:** SignalEvent (without signalId, ingestedAt, status - server-generated)
- **Response:** `{ signalId, correlationGroupId?, status }`
- **Rate limit:** 1000 req/sec per org

### GET /v1/signals/{signalId}

- **Auth:** SigV4
- **Response:** Full SignalEvent with enrichment data

### POST /v1/personas

- **Auth:** SigV4
- **Body:** Persona configuration
- **Response:** `{ personaId }`

### PUT /v1/personas/{personaId}

- Update persona config (subscriptions, suppression rules, delivery prefs)

### POST /v1/personas/{personaId}/subscribe

- **Body:** `{ naturalLanguage: "Notify me when..." }`
- **Response:** `{ subscriptionId, compiledPattern }`

### POST /v1/feedback

- **Body:** `{ deliveryId, feedback: "useful" | "noise" | "escalate" }`

### GET /v1/analytics/nrs

- **Query:** `orgId, timeRange, personaId?`
- **Response:** Notification Reduction Score, MTTA, volume trends

---

## Infrastructure (CDK)

### Stacks

1. **IngestionStack**- API Gateway (Publish API endpoint)
- Kinesis Data Stream (signal buffer)
- EventBridge rules (AWS service signal capture)
- Lambda (webhook adapters for 3P: Datadog, PagerDuty, ServiceNow)
- DynamoDB table (SignalEvents)
2. **IntelligenceStack**- Lambda (Correlation Engine - processes Kinesis stream)
- DynamoDB table (CorrelationGroups)
- Lambda (Severity Scorer - invokes Bedrock Nova Pro)
- Bedrock Knowledge Base (AWS documentation RAG)
- Lambda (Enrichment - Resource Explorer + RAG)
3. **PersonaStack**- DynamoDB table (Personas)
- Lambda (Audience Router - matches signals to personas)
- Step Functions (Orchestration workflow: match → suppress? → transform → deliver)
- Lambda (Content Transformer - invokes Bedrock Claude for per-persona rewrite)
- Bedrock Agent (NL Subscription Manager)
4. **DeliveryStack**- SES (email templates + sending)
- Lambda (Slack/Teams delivery via Chatbot API)
- EventBridge Scheduler (escalation timers)
- Lambda (Escalation Handler)
- DynamoDB table (DeliveryRecords)
- API Gateway (Action callback endpoints for embedded buttons)
5. **LearningStack**- DynamoDB Streams → Lambda (feedback processor)
- S3 (training data for suppression model)
- SageMaker endpoint (behavioral suppression model) [Phase 3]
- Lambda (NRS calculator - scheduled)
6. **AnalyticsStack**- S3 (audit trail - immutable logs)
- Athena (query layer for compliance reports)
- CloudWatch custom metrics (MTTA, MTTR, NRS)
- Lambda (Monthly executive report generator via Bedrock)

---

## Implementation Order

### Phase 1: MVP (4 weeks in Kiro)

1. IngestionStack - Publish API + EventBridge capture for CloudWatch alarms + Security Hub findings
2. PersonaStack (basic) - 3 hardcoded personas (CISO, SRE, CTO), Bedrock content transformation
3. DeliveryStack (email only) - SES delivery with persona-specific HTML templates
4. Basic DynamoDB tables + audit trail

### Phase 2: Platform (3 weeks)

1. Correlation Engine (Kinesis + time-window grouping)
2. Escalation chains (EventBridge Scheduler)
3. Slack delivery via Chatbot
4. Publish API SDK (Python package)

### Phase 3: Intelligence (3 weeks)

1. Adaptive learning (feedback collection + suppression model)
2. NRS analytics dashboard (CloudWatch + custom metrics)
3. 3P webhook adapters (PagerDuty, Datadog)
4. Compliance audit trail (S3 + Athena)

### Phase 4: Scale (2 weeks)

1. NL Subscription Manager (Bedrock Agent)
2. Predictive signals (CloudWatch ML trend analysis)
3. Cross-account Organizations support
4. Load testing + multi-tenant isolation validation

---

## Testing Strategy

- **Unit tests:** Each Lambda handler tested with mocked Bedrock/DDB responses
- **Integration tests:** CDK integ tests deploying to sandbox account
- **E2E tests:** Publish signal → verify delivery record created within SLA
- **Load tests:** Artillery/k6 against Publish API endpoint (target: 1000 req/sec)
- **Persona quality tests:** Golden set of signals with expected per-persona outputs, evaluated by Bedrock judge

---

## Key Design Decisions

1. **Kinesis over SQS:** Need ordered processing for time-window correlation
2. **Step Functions over direct Lambda chaining:** Visibility, retry logic, human-readable workflow
3. **DynamoDB over Aurora:** Predictable latency at scale, per-org table isolation, TTL for correlation windows
4. **Bedrock over custom models:** No training data needed, rapid iteration on prompts, multi-model flexibility
5. **Two-tier latency:** P1/P2 use pre-built templates (< 3s); P3+ use full Bedrock generation (< 15s)



---

## Phase 2 Context (for Kiro Memory)

### What's Already Built (Phase 1 - COMPLETE)
- IngestionStack: API Gateway + Kinesis + DynamoDB SignalEvents + EventBridge rules (CloudWatch, Security Hub, Health)
- IntelligenceStack: Correlation table + Correlator Lambda (Kinesis consumer, time-window grouping, triggers SFN)
- PersonaStack: Persona DynamoDB table (seeded CISO/SRE/CTO), Audience Router, Content Transformer (Bedrock), Email Sender, Step Functions workflow (Route → Transform → Map[Deliver])
- DeliveryStack: DeliveryRecords table (GSIs by-signal, by-persona), Action Callback Lambda + API Gateway
- Shared Lambda Layer: bundled with pydantic, ulid-py, structlog, boto3 (uses local pip3 bundling, not Docker)

### Phase 2 Architecture Decisions
- **Escalation uses EventBridge Scheduler** (not SFN Wait states) - durable, survives Lambda cold starts, can be cancelled when ACK received
- **Slack via AWS Chatbot** - not direct Slack API - keeps everything AWS-native
- **Channel branching in Step Functions** - Choice state after Transform step routes to email/slack/both based on persona preferences
- **SDK uses botocore SigV4** - not requests library - for proper AWS auth

### File Naming Conventions
- Lambda handlers: `src/{layer}/{function_name}.py` with a `handler(event, context)` function
- CDK stacks: `infra/lib/{stack-name}-stack.ts`
- Tests: `tests/unit/test_{handler_name}.py`
- All handlers import from `shared.config` and use `structlog` for logging

### Environment Variables Pattern
Each Lambda receives: `STAGE`, `{TABLE}_TABLE_NAME`, and any service-specific vars (e.g., `SES_DOMAIN`, `PERSONA_WORKFLOW_ARN`, `CALLBACK_API_URL`)
