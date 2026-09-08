# AWS Pulse code and feature review

Review date: 8 September 2026. Baseline: `4433f3b0b213ac8dfd8af2ed10970d446a870cd3` on `main`.

## Verdict

**Not all advertised features work. Do not treat this as production-ready or a completed Phase 4 platform.** The proposed branch repairs reproducible defects in the existing core flow and adds service-boundary regression coverage. It does not implement the missing product capabilities below or certify a live AWS deployment.

The original 97 Python tests passed with manually configured import paths, region, and dummy credentials, despite serious wiring defects. With the documented plain pytest invocation, collection/import failures occurred. TypeScript compilation failed on `cdk.Aws.ORGANIZATION_ID`; `npm test` failed because no Jest tests existed. The latest baseline GitHub CI run failed at lint before tests/synthesis, and the latest deployment run failed at AWS credential configuration before deployment.

- [Baseline CI run](https://github.com/ShankManok/aws-pulse/actions/runs/31374724922)
- [Baseline deployment run](https://github.com/ShankManok/aws-pulse/actions/runs/31374725538)

## Defects repaired in this branch

| Priority | Finding and consequence | Repair and verification |
|---|---|---|
| P0 | Signal producers wrote snake_case keys to a table requiring `signalId` / `ingestedAt`, causing persistence failure. Nested prediction floats were also invalid DynamoDB values. | Separate canonical JSON event serialization from DynamoDB serialization; retain deployed keys and convert nested floats to Decimal. Moto tests use the deployed primary-key schema. |
| P0 | AWS-native rules sent raw events directly to Kinesis while the correlator expects canonical signals. Forwarded events could match both paths. | One Lambda normalization route for local and forwarded events. Preserve CloudWatch state and top-level resource ARNs, and process each finding in a Security Hub batch. Template and service-boundary tests cover these paths. |
| P0 | TypeScript referenced a nonexistent `Aws.ORGANIZATION_ID` constant. | Explicit optional `organizationId` context, validated and used only when provided. No cross-account bus policy by default. |
| P0 | The escalation workflow had a CloudFormation dependency cycle even after CDK synthesis succeeded. | Use the known state-machine physical name to construct the escalation target ARN. A regression traverses references and dependencies in all six generated templates. |
| P0 | Correlator updates lacked `dynamodb:UpdateItem` permission. | Grant read/write access to the signal table; assert the generated policy. Update failures now report a failed stream record. |
| P0 | Missing webhook credentials disabled authentication on public endpoints. | All three integrations reject unauthenticated requests when unconfigured. Constant-time comparison for Datadog and ServiceNow credentials. |
| P0 | Anyone with a predictable delivery ID could mutate feedback through unauthenticated GET/POST; email scanners could acknowledge an alert. | Random delivery-scoped tokens, hashed storage, 24-hour expiry, read-only GET confirmation, and conditional POST mutation with replay protection. Invalid, expired, missing and cross-delivery tokens are rejected. Existing untokenized links stop working. |
| P0 | Subscription management was public; Publish API used a usage key without IAM authorization, while the SDK sent only SigV4. | Require IAM on publish and subscription management. Publish retains its API usage key; SDK now accepts `api_key` and supplies both credentials. Auth contracts are tested. |
| P0 | Learned suppression could hide every source, including urgent alerts, after three noise responses. Floating confidence values also failed DynamoDB writes. | Store real source on deliveries; learn per source; ignore legacy global rules and source-less records; never apply learned suppression to high/critical alerts. Store confidence as Decimal. This remains a basic heuristic, not a validated learning model. |
| P1 | Claude had an incomplete model ID; Nova was sent Anthropic's model-specific request body. | Configurable complete model ID and Bedrock Converse requests for both model families. Botocore stub validation covers the request/response shape. Regional model access remains a live setup requirement. |
| P1 | Escalation targets such as `persona-cto` were not resolved and default routing could notify other personas. | Escalation input targets only its designated existing persona. Normal routing accepts full persona IDs. |
| P1 | Channel fallback sent to members who opted out of that channel. | Empty channel recipient lists are skipped instead of overridden. |
| P1 | Transformation, delivery, schedule-creation and escalation workflow-start failures could appear as successful tasks. | These failures now raise so the orchestration does not report success. No-recipient results contain the `delivery_ids` field expected by Step Functions. Retry/idempotency limitations remain below. |
| P1 | Workflow names depended on the current second; retries could create additional executions, and separate escalations could collide. Truncated schedule names could also collide. | Hash-based execution and schedule identifiers include signal or delivery identity. This does not provide end-to-end exactly-once delivery. |
| P1 | Callback URLs contained a double slash after the stage; email content was interpolated as HTML. | Normalize the base URL and HTML-escape untrusted notification text and links. |
| P1 | Test commands, missing Jest tests, undeclared ts-node usage and lint errors blocked validation. Dev deployment ran independently of CI success. | Repository-local test configuration, dummy AWS test credentials, TypeScript build before CDK, five infrastructure contract tests, lint fixes, and both Python suites in CI. Dev deployment is now manual and runs validation first. |
| P2 | SDK query parameters were not URL encoded; credentials were frozen once for the lifetime of the client. | Encode query strings and refresh frozen credentials when signing each request. Load-test requests also sign with SigV4. No live load test was run. |

## Feature verification matrix

“Local” means mocked AWS responses, Moto service emulation, SDK shape validation, or generated-template inspection. It does not mean delivered in a real account.

| Feature | Status after proposed fixes | Limit or missing work |
|---|---|---|
| Publish API and canonical persistence | Local flow passes | Real API Gateway authentication and DynamoDB/Kinesis writes need a sandbox smoke test. No atomic outbox or producer idempotency. |
| CloudWatch and Security Hub ingestion | Local normalization/persistence passes | Batch processing and state/ARN handling tested. Native-event replay still needs a stable producer idempotency strategy. |
| Health, GuardDuty, Config and cross-account ingestion | Basic adapter code exists; existing unit tests pass | Full provider event fixtures, valid resource identities and member-account deployment remain unverified. Org forwarding rules are per account, not automatic organization-wide rollout. |
| PagerDuty, Datadog, ServiceNow | Mapping/auth unit tests pass; missing-secret rejection covered | CDK still sets empty webhook credentials. Provision secrets securely before enabling providers. Base64 bodies, malformed authenticated payloads and provider replay controls need further hardening. |
| Resource correlation | Local grouping passes | Groups exact ARN sets, not overlapping resources; concurrent read/modify/write can lose members; group IDs are reused after the time window. No causality analysis or content deduplication. |
| AI severity and enrichment | Incomplete | Severity helper exists but is not wired into the pipeline. No `severity_scorer.py`, Resource Explorer enrichment, RAG/knowledge base or causal assessment. |
| Persona content transformation | Local flow and Bedrock request shape pass | Live model/inference-profile access and output quality not tested. No urgent template-only path. |
| Persona routing | Default rules and explicit escalation targeting pass locally | No tenant/account-ownership authorization boundary. Subscription scans are unpaginated; ordinary read errors are still swallowed. |
| Natural-language subscriptions | Parser/filter unit tests pass | A Lambda model call, not a deployed Bedrock Agent or compiled EventBridge subscription. Fallback keywords can broaden intent; unknown personas can be upserted; filters/limits need stricter validation. |
| Email delivery | Mocked SES call, HTML, delivery row and feedback flow pass | SES domain defaults to an example domain and members use example addresses. Delivery acceptance and inbox receipt are unverified. Retries can resend and overwrite prior delivery feedback. |
| Slack delivery | SNS payload/unit tests pass | **Recipient routing incomplete:** every recipient is published to the same SNS topic without selecting a channel. No Chatbot Slack configuration is provisioned. Multiple recipients can produce repeated broadcasts. |
| Feedback buttons | Authenticated confirmation and acknowledgement tested locally | “Suppress” records noise feedback rather than immediately creating a scoped rule. “Escalate” records a request; there is no immediate escalation dispatcher. UI/API wording must reflect this until implemented. |
| Automatic escalation | One next-persona workflow tested locally; failure propagation repaired | Further chain hops are deliberately disabled. SLA hint is not used in preference selection. Missing-record/lookup errors and concurrent acknowledgement races need durable state/retry handling. |
| Learned suppression | Source scope and urgent-alert protections pass locally | Counts deliveries, not distinct incidents; ignores useful-feedback ratio; appends duplicate rules; only reads one query page. Nightly job prunes rather than retrains. Manual suppression is not protected by the learned-rule guard. |
| Predictive signals | Regression math tests and decimal persistence pass | Predictors need configuration; scans are unpaginated. Cross-account metrics, region selection, stale data and prediction quality are unverified. |
| NRS and MTTA | Existing arithmetic tests pass; **NRS semantics are wrong** | `correlated` is counted as deduplicated even though every correlated signal starts a delivery workflow. Suppression decisions are not recorded back to signal status. Single-page scans undercount at scale. Do not present NRS as achieved noise reduction. |
| Audit export/Athena | Export code, bucket and workgroup exist | Not immutable: bucket versioning is off and hourly objects overwrite. Boundary-inclusive scans can duplicate records; late acknowledgement changes are missed; partial scan errors can yield incomplete exports. Athena table is a manual SQL artifact. |
| Teams, console, mobile, SMS | Not implemented as dedicated delivery paths | Unknown channels currently fall back to email in the workflow. |
| Digest, quiet hours, cadence | Not implemented | Config fields exist but are not enforced. |
| Dashboard, monthly reports, MTTR | Not implemented | No dashboard resources, report generator or MTTR computation. |
| Multi-tenancy | Not implemented securely | Shared tables and global persona scans are not organization isolation. Restrict to a single trusted organization during development. |
| 3s/15s latency, 99.9% uptime, 100K/day scale | Unverified targets | Kinesis batches can wait five seconds and every persona uses Bedrock. Existing Locust scripts are not performance evidence. |

## SDK/API availability

The SDK is broader than the deployed API. Fixing signing does not make missing methods available.

| SDK method / route | Implemented backend? |
|---|---|
| `publish_signal` / `POST /v1/signals` | Yes; now requires SigV4 plus API key |
| `get_signal` / `GET /v1/signals/{id}` | No |
| `create_persona` / `POST /v1/personas` | No |
| `update_persona` / `PUT /v1/personas/{id}` | No |
| `list_deliveries` / `GET /v1/deliveries` | No |
| `submit_feedback` / `POST /v1/feedback` | No; tokenized action callbacks use a different API |
| `POST /v1/personas/{id}/subscribe` | Yes, separate Persona API, SigV4 required; SDK has no dedicated method |
| `GET /v1/analytics/nrs` | No |

## Validation and release gates

Run `pytest`, `make lint`, and `npm test` after installing requirements and `npm ci`. Python tests always use dummy credentials. They do not send notifications or make live AWS calls. `npm test` compiles TypeScript, synthesizes six CDK stacks, and checks generated authentication, normalization, permission and dependency contracts.

**Local Python result: 137 tests passed, 81% statement coverage of `src`.** This is not branch coverage or proof of production readiness. The audit exporter has no automated test coverage in this suite; its limitations above are static-review findings. **Infrastructure: five contract tests passed after synthesis of all six core stacks.** Ruff, TypeScript compilation and `git diff --check` passed. No live AWS deployment, SES/SNS delivery, Bedrock output quality evaluation, API authorization probe or load test was performed. The failed baseline deployment only proves that that GitHub run did not deploy; it does not prove that the user has never deployed the project elsewhere.

Before a sandbox deployment: configure the AWS deployment role/OIDC trust, a verified SES sender and real members, an available Bedrock model/inference profile, webhook credentials, and the intended Chatbot routing. Before production: resolve the remaining P0/P1 product and reliability gaps, add durable ingest/delivery idempotency and account/tenant isolation, then run end-to-end and failure/load tests with agreed thresholds.

Compatibility changes: existing unsigned API-key-only publish callers must adopt SigV4; SDK publish callers must provide the usage key; subscription callers must sign; old untokenized action links are rejected; action links expire after 24 hours and accept one response. The existing deployed DynamoDB key schema is preserved. Dev deployment no longer starts automatically on a push to `main`.

## AWS references checked

- [CloudWatch alarm event format](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-and-eventbridge.html), including top-level resource ARNs and alarm state details.
- [Claude 4 on Amazon Bedrock](https://aws.amazon.com/blogs/aws/claude-opus-4-anthropics-most-powerful-model-for-coding-is-now-in-amazon-bedrock/), including the complete Sonnet 4 model ID and Converse API recommendation. Model availability and inference profiles must still be checked in the deployment region/account.
