# AWS Pulse implementation and readiness review

Updated 8 September 2026. Branch: `review/verify-core-flows`, [pull request #1](https://github.com/ShankManok/aws-pulse/pull/1). The [initial review](review-initial.md) preserves the baseline findings and the first repair pass.

## What “100%” means here

The application and Python SDK have **100% statement coverage**, enforced by CI with `--cov-fail-under=100`. There are no coverage omissions or `pragma: no cover` exceptions. This is **not** 100% branch coverage, a claim of zero defects, completion of the engineering specification, or certification of a deployed AWS system.

Local acceptance checks cover 183 Python tests, nine infrastructure contracts, TypeScript compilation, six synthesized stacks and imports of all 21 packaged Python Lambda handlers using only their deployment assets. AWS interactions use Moto, mocks or SDK schema validation. No live notifications, deployment, merge, load test or model-quality evaluation has been performed.

## Repairs completed

| Area | Verified behavior |
|---|---|
| Ingestion | Canonical DynamoDB keys and Decimal values; one normalized native-event route; GuardDuty resource identities retain account/region; all Security Hub findings are processed. |
| Durable publication | Producers persist a signal once. A DynamoDB stream outbox forwards it to Kinesis, reports partial failures and retains exhausted events in an SQS failure queue. Receipt records and status updates are not published as new signals. |
| Producer retries | Publish supports an `Idempotency-Key`, transactionally creating a signal and receipt. Reusing a key with different content is rejected. The SDK retains one key through its retries. Native event IDs and webhook payload digests provide replay keys. Receipts have a 30-day TTL. |
| Public APIs | All SDK routes now have backends on the main API. IAM and usage keys protect those routes; application checks bind callers to the deployment account. Persona configuration is validated and updates use optimistic version checks. Read APIs filter organization ownership and paginate results. |
| Webhooks | Secrets Manager holds provider credentials. Empty credentials reject requests. Base64 bodies, invalid authenticated payloads and provider signature variants are tested. |
| Correlation | Exact resource sets use deterministic event-time windows and atomic set additions. Concurrent updates cannot replace another worker's membership list. |
| Routing | Organization and account ownership checks; paginated subscription reads; failures propagate. Strict subscription filters reject invalid AI output rather than broadening it. Custom personas can be selected by ID or subscription. |
| Delivery | Stable recipient-based IDs survive reordering. Completed sends replay without resetting feedback. A prior uncertain send blocks automatic resend. Slack requires an explicit recipient-to-SNS-topic mapping. Unsupported channels fail rather than fall back to email. |
| Feedback | Expiring delivery-scoped tokens; GET confirms without mutation; conditional POST protects replay. IAM feedback supports acknowledgement and resolution timestamps. Suppress creates a scoped 24-hour rule for nonurgent signals; escalate invokes the configured next-persona workflow immediately. |
| Escalation | The full chain survives subsequent hops. Workflow names remain stable on retry. Failed starts and state writes fail the invocation. The delivery is marked escalated after workflow acceptance. |
| Learning | Evidence is counted per distinct signal/source, not per recipient. A useful response overrides noise for that signal. Learning requires at least three noise incidents and an 80% noise ratio. One map entry per source replaces repeated appended rules. Useful feedback can remove a learned rule. Learned suppression never hides high or critical signals. |
| Metrics | UTC calendar-day boundaries, pagination and tenant filters. Receipts and correlated-but-delivered signals do not count as deduplication. Only fully suppressed routing decisions are written as suppressed. MTTA ignores malformed or negative durations. Failed writes propagate. |
| Audit | Every delivery insertion, update and deletion is exported from its DynamoDB stream. Content-addressed hourly reconciliation snapshots use half-open time windows. Conditional S3 writes prevent replay overwrites; versioning and 365-day compliance Object Lock retain object versions. Failure queues preserve exhausted stream records. |
| CI | Lint, Python/SDK coverage gate, TypeScript build, infrastructure contracts and packaged-handler import checks. Dev deployment remains manually dispatched and test-gated. Each deployer supplies their own `AWS_ROLE_ARN` environment secret; no account-specific ARN or credentials are committed. |

## SDK/API availability

Main API routes require both IAM SigV4 and `x-api-key`:

| Method | Route |
|---|---|
| `publish_signal` | `POST /v1/signals` |
| `get_signal` | `GET /v1/signals/{signalId}` |
| `create_persona` | `POST /v1/personas` |
| `update_persona` | `PUT /v1/personas/{personaId}` |
| `subscribe` | `POST /v1/personas/{personaId}/subscribe` |
| `list_deliveries` | `GET /v1/deliveries` |
| `submit_feedback` | `POST /v1/feedback` |
| `get_nrs` | `GET /v1/analytics/nrs` |

The older standalone subscription API still requires IAM. Tokenized notification callbacks use the callback API. See [configuration and recovery](operations.md).

## Feature verification matrix

These remaining items prevent a claim of full product or production readiness:

| Capability | Remaining scope |
|---|---|
| Correlation and deduplication | Exact-set grouping is implemented. Partial resource overlap, causal analysis and semantic/content deduplication are not. Producer replay suppression is not semantic deduplication. |
| Intelligence | Bedrock content transformation and metric extrapolation are implemented. AI severity scoring remains an unwired helper. Resource Explorer/RAG enrichment, causal assessment and model-quality evaluation remain. |
| Delivery channels and preferences | Email and configured Slack destinations support realtime delivery. Teams, console, mobile, SMS, digests, quiet hours and other cadences remain unsupported and are rejected. |
| Escalation timing | An acknowledgement can race a workflow start. SES/SNS cannot provide exactly-once external delivery. Reconciliation of uncertain sends remains an operator task. |
| Predictions | No stale-data policy, automatic cross-account metric querying or forecasting-quality validation. Predictor configurations must be populated explicitly. Scheduled prediction retries do not share a producer idempotency key. |
| Learning | A tested heuristic, not a trained model. Nightly maintenance prunes legacy rules; it does not retrain. Map-based rules are ignored after expiry but need periodic storage compaction for long-running deployments. |
| Analytics | NRS/MTTA and resolution timestamps exist. MTTR aggregation, dashboards, monthly AI reports and Glue/Athena table provisioning remain. |
| Tenant model | One trusted organization per isolated deployment. This is not a shared SaaS tenant boundary, and cross-account API callers must assume a role in the central deployment account. |
| Organization rollout | Opt-in organization bus policy and per-account forwarding stack exist. Automatic organization-wide rollout remains. |
| Production evidence | IAM/API Gateway smoke tests, SES inbox receipt, configured Chatbot destinations, regional Bedrock access, retention verification, DLQ recovery, realistic load and latency/SLO measurements must run in a configured AWS sandbox. |

## Acceptance commands

```bash
make lint
make test
npm test
python scripts/package_smoke.py infra/cdk.out
```

Do not equate these local results with the 3-second/15-second latency, 99.9% uptime or 100K-signals/day targets in the engineering specification.
