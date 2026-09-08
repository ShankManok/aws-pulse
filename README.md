# AWS Pulse

Intelligent AWS notification infrastructure: ingest signals, group related resources, transform content for personas, deliver notifications, and collect feedback.

**Status: development prototype with tested core components, not a complete production platform.** Read the [code and feature review](docs/review.md) for verified behavior, repaired defects, missing features and release gates. The engineering spec describes the target product, not completed functionality.

## Local development

Requires Python 3.12 and Node.js 20 or later.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
npm ci
make test
make lint
npm test
python scripts/package_smoke.py infra/cdk.out
```

`make test` runs unit and simulated integration tests with dummy AWS credentials and enforces 100% statement coverage for the application and SDK. `npm test` builds TypeScript, synthesizes the CDK templates, and checks infrastructure contracts. No live notifications are sent by these commands. Synthesis bundles Python dependencies and therefore needs package-index access.

## Current components

| Directory | Contents |
|---|---|
| `infra/` | Six core CDK stacks plus optional per-account organization forwarding |
| `src/ingestion/` | Publish handler, native-event normalization, three webhook adapters |
| `src/api/` | IAM-bound persona, signal, delivery, feedback and analytics APIs |
| `src/intelligence/` | Resource grouping and metric-trend predictor |
| `src/persona/` | Audience routing, Bedrock transformation and NL subscription parser |
| `src/delivery/` | SES/SNS senders, tokenized feedback and escalation handlers |
| `src/learning/` | Feedback heuristic, suppression, daily metrics and retained delivery-change audit export |
| `src/shared/` | Canonical models, DynamoDB serialization, Bedrock client and action tokens |
| `sdk/python/` | Python client with implemented publish, persona, subscription, read, feedback and analytics routes |
| `tests/` | Unit tests, simulated integration regressions and unexecuted live load scripts |

All main SDK/API routes require IAM SigV4 authorization and an API usage key. Webhooks reject requests until credentials are configured. Email links show a confirmation page before recording feedback.

The tested core now has transactional ingestion, replay-aware delivery, full escalation chains, strict subscription validation and retained audit history. Remaining target features include semantic deduplication, enrichment, shared SaaS tenancy, cadence/quiet hours, additional channels and dashboards. See the [feature matrix](docs/review.md#feature-verification-matrix) and [configuration/recovery guide](docs/operations.md).

## Deployment

Resolve the review's release gates and configure a sandbox account first. The GitHub dev deployment workflow is manually dispatched and validates the code before deployment. A passing local suite does not verify SES identities, model access or Slack routing.

```bash
npm run synth
# After account configuration and readiness review:
make deploy-dev
```

For cross-account ingestion, supply `--context organizationId=o-...` to the central ingestion stack. Without it, no cross-account bus access is granted. Forwarding must be installed in each participating account; the optional OrgSetup stack does not automatically roll out to an organization.

[API reference](docs/api-reference.md) · [Deployment notes](docs/deployment.md) · [Engineering target](.kiro/spec.md)

Apache-2.0.
