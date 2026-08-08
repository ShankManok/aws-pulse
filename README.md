# Pulse

> Intelligent notification infrastructure for AWS — ingests signals from all services, correlates across boundaries, transforms per-persona using AI, and delivers through the right channel at the right time.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         AWS Pulse                                │
│                                                                         │
│  ┌──────────┐   ┌──────────────┐   ┌────────────┐   ┌──────────────┐  │
│  │ Ingestion│──▶│ Intelligence │──▶│  Persona   │──▶│   Delivery   │  │
│  │  Layer   │   │    Engine    │   │   Engine   │   │    Layer     │  │
│  └──────────┘   └──────────────┘   └────────────┘   └──────────────┘  │
│       ▲                                                     │          │
│       │              ┌──────────────┐                       │          │
│       └──────────────│   Learning   │◀──────────────────────┘          │
│                      └──────────────┘                                  │
└─────────────────────────────────────────────────────────────────────────┘
```

## Quick Start

```bash
# Prerequisites
npm install -g aws-cdk
pip install boto3

# Clone and install
git clone https://github.com/shankmanok/pulse.git
cd aws-pulse
npm install

# Deploy to dev
cdk deploy --all --context stage=dev
```

## Project Structure

```
aws-pulse/
├── .kiro/
│   └── spec.md                    # Kiro engineering spec (this drives development)
├── infra/                         # CDK infrastructure
│   ├── bin/
│   │   └── app.ts                 # CDK app entry point
│   ├── lib/
│   │   ├── ingestion-stack.ts     # API GW, Kinesis, EventBridge rules
│   │   ├── intelligence-stack.ts  # Correlation, Severity, Enrichment
│   │   ├── persona-stack.ts       # Personas, Content Transformer, Router
│   │   ├── delivery-stack.ts      # SES, Chatbot, Escalation, Actions
│   │   ├── learning-stack.ts      # Feedback, Suppression model
│   │   └── analytics-stack.ts     # Audit trail, NRS, Reports
│   ├── cdk.json
│   ├── tsconfig.json
│   └── package.json
├── src/                           # Lambda handlers (Python)
│   ├── ingestion/
│   │   ├── publish_handler.py     # Publish API Lambda
│   │   ├── normalizer.py          # Signal normalization
│   │   └── webhook_adapters/
│   │       ├── datadog.py
│   │       ├── pagerduty.py
│   │       └── servicenow.py
│   ├── intelligence/
│   │   ├── correlator.py          # Kinesis consumer - time-window grouping
│   │   ├── severity_scorer.py     # Bedrock severity assessment
│   │   ├── enricher.py            # Resource Explorer + RAG enrichment
│   │   └── predictor.py           # Trend-based predictive signals
│   ├── persona/
│   │   ├── audience_router.py     # Match signals to personas
│   │   ├── content_transformer.py # Bedrock per-persona content generation
│   │   ├── subscription_agent.py  # NL subscription → EventBridge rules
│   │   └── prompts/
│   │       ├── ciso.txt
│   │       ├── sre.txt
│   │       ├── cto.txt
│   │       ├── finops.txt
│   │       └── compliance.txt
│   ├── delivery/
│   │   ├── email_sender.py        # SES HTML email delivery
│   │   ├── slack_sender.py        # Chatbot Slack delivery
│   │   ├── escalation_handler.py  # SLA timer + escalation logic
│   │   ├── action_handler.py      # Embedded button callbacks
│   │   └── templates/
│   │       ├── digest.html
│   │       ├── alert.html
│   │       └── escalation.html
│   ├── learning/
│   │   ├── feedback_processor.py  # DDB Streams consumer
│   │   ├── suppression_model.py   # Behavioral suppression logic
│   │   └── nrs_calculator.py      # Notification Reduction Score
│   └── shared/
│       ├── models.py              # Pydantic data models
│       ├── bedrock_client.py      # Bedrock invocation wrapper
│       └── config.py              # Environment config
├── sdk/                           # Pulse SDK
│   └── python/
│       ├── pulse/
│       │   ├── __init__.py
│       │   ├── client.py          # publish_signal(), create_persona()
│       │   └── models.py          # SDK data models
│       ├── setup.py
│       └── README.md
├── tests/
│   ├── unit/
│   │   ├── test_correlator.py
│   │   ├── test_severity_scorer.py
│   │   ├── test_content_transformer.py
│   │   └── test_audience_router.py
│   ├── integration/
│   │   ├── test_publish_flow.py
│   │   └── test_delivery_flow.py
│   └── e2e/
│       └── test_signal_to_delivery.py
├── docs/
│   ├── architecture.md
│   ├── api-reference.md
│   ├── personas.md
│   └── deployment.md
├── .github/
│   └── workflows/
│       ├── ci.yml                 # Lint + unit tests
│       ├── deploy-dev.yml         # Deploy to dev on push to main
│       └── deploy-prod.yml        # Deploy to prod on release tag
├── .gitignore
├── package.json
├── requirements.txt
├── Makefile
└── README.md
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Infrastructure | AWS CDK (TypeScript) |
| Compute | AWS Lambda (Python 3.12) |
| Event Bus | Amazon EventBridge |
| Stream Processing | Amazon Kinesis Data Streams |
| AI/ML | Amazon Bedrock (Claude 4 Sonnet, Nova Pro) |
| Orchestration | AWS Step Functions |
| Database | Amazon DynamoDB |
| Delivery | Amazon SES, AWS Chatbot, User Notifications |
| Escalation | EventBridge Scheduler |
| Analytics | CloudWatch Metrics, Athena, S3 |
| API | Amazon API Gateway (REST) |
| Auth | IAM (SigV4) + API Keys (3P) |

## Development

```bash
# Run unit tests
make test

# Deploy single stack
cdk deploy IngestionStack --context stage=dev

# Invoke locally
sam local invoke PublishHandler -e events/sample_signal.json

# Run E2E
pytest tests/e2e/ --stage=dev
```

## License

Apache-2.0
