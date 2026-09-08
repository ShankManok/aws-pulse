# Configuration and recovery

## Deployment configuration

Use an isolated sandbox deployment for one trusted organization. The default organization identifier is `default`; physical table names are separated by stage/account. IAM-authenticated API callers and declared signal account IDs are restricted to the central deployment account. AWS-native organization forwarding uses the separately configured EventBridge organization policy.

Before a live smoke test:

- Set `sesDomain` CDK context to a verified SES domain and replace seeded example recipients through the persona API. SES identities and inbox receipt are not provisioned or verified by these tests.
- Set `slackDestinations` CDK context to a JSON object mapping each member's Slack recipient identifier to its authorized SNS topic ARN. Connect those topics to the intended Chatbot channels. An unconfigured recipient fails before a send claim is created.
- Populate the Secrets Manager secret identified by `WebhookSecretArn` with `PAGERDUTY_WEBHOOK_SECRET`, `DATADOG_WEBHOOK_API_KEY`, `SERVICENOW_WEBHOOK_USER` and `SERVICENOW_WEBHOOK_PASS` as needed. Do not put credential values in CDK context or source control.
- Configure regional Bedrock model access and predictor rows. Content transformation uses `BEDROCK_MODEL_ID` when set in the Lambda environment; the default complete model identifier is in `shared/bedrock_client.py`.
- Give API callers the necessary `execute-api:Invoke` permissions and the API usage-key value, not merely the key ID.

The audit bucket enables versioning and **365-day compliance retention** for new object versions. Review that retention requirement before deploying. This code change has not deployed or modified any AWS retention policy.

## Supported persona configuration

Management APIs accept `email` and `slack`, `cadence: realtime`, no quiet hours, and `escalationAfterMinutes` between 1 and 10080. Unsupported options return a validation error. Each member specifies a `principalId` and allowed channels. New custom personas need an explicit audience hint using their returned ID or a matching subscription; the default severity rules target the seeded persona IDs.

Subscriptions require a nonempty filter; malformed model output produces an error without saving a broadened rule. A persona is limited to 100 subscriptions. Updates may include the last known `version` to detect concurrent edits. Server-managed organization, subscription and suppression fields cannot be overwritten through ordinary persona updates.

## Publish retries

The SDK does not automatically retry non-idempotent persona creation or subscription POSTs after an ambiguous failure. It retries reads/updates and idempotent publishes.

The SDK generates an idempotency key per logical publish call and retains it across HTTP retries. Pass `idempotency_key` explicitly to reuse it across process restarts. A key binds to the complete canonical payload for that deployment; changing the payload requires a new key. Receipts have a 30-day TTL, with deletion subject to DynamoDB TTL processing.

A successful publish acknowledges durable database acceptance, not stream processing or notification delivery. Monitor the outbox failure queue and downstream workflow failures. Replaying an outbox record retains the signal ID; workflow execution naming prevents a second normal execution for that ID within Step Functions' name-retention period.

## Uncertain external sends

A delivery row with `sendStatus: sending` and no `deliveredAt` may represent either a failed send or an accepted notification whose completion write failed. The worker raises `DeliveryUncertain` rather than blindly sending again.

An operator should inspect provider evidence and the workflow execution. If acceptance is established, conditionally record `sendStatus: sent` and the actual `deliveredAt`, preserving token and feedback fields, then retry the failed task. If nonacceptance is established, record the reconciliation decision in the incident record before resetting the claim for a deliberate retry. If the result is unknown, keep it uncertain. Never bulk-delete claims to clear workflow errors.

This process is deliberately not described as exactly-once delivery. Failed-record queues and audit streams must be monitored and replayed before their retention expires. A late acknowledgement can still race an escalation that has already started.

## Live acceptance checklist

1. Verify allowed and denied IAM calls through the actual API Gateway endpoints.
2. Publish an idempotent signal twice and follow its database, outbox, workflow and delivery records.
3. Confirm a real SES email and a separately mapped Slack notification. Confirm that unrelated recipients do not receive them.
4. Exercise acknowledgement, scoped suppression, useful-feedback reversal and a full multi-hop escalation.
5. Introduce a controlled provider failure; verify visible task failure, failure-queue capture and recovery without duplicate accepted sends.
6. Verify delivery updates in retained audit objects and compare daily metrics with source records.
7. Measure latency and load against explicitly agreed targets.

These steps have not been run against live AWS in this review.
