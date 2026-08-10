# Deployment Guide

## Prerequisites

- AWS CLI v2 configured with credentials
- Node.js 18+ and npm
- Python 3.12 (for Lambda runtime) and pip3
- AWS CDK CLI: `npm install -g aws-cdk`
- An AWS account with permissions for: Lambda, DynamoDB, Kinesis, S3, SES, API Gateway, Step Functions, EventBridge, IAM, CloudWatch, SNS, Athena, SQS

## Initial Setup

```bash
# Clone the repository
git clone <repo-url>
cd aws-pulse

# Install Python dependencies
pip3 install -r requirements.txt -r requirements-dev.txt

# Install CDK dependencies
cd infra && npm install && cd ..

# Bootstrap CDK (first time only)
cd infra && npx cdk bootstrap --context stage=dev && cd ..
```

## Deploy All Stacks

```bash
cd infra
npx cdk deploy --all --context stage=dev --require-approval never
```

This deploys the following stacks in dependency order:
1. `Pulse-Ingestion-dev` — API Gateway, Kinesis, Signal DynamoDB table
2. `Pulse-Delivery-dev` — Delivery DynamoDB table, Action Callback API
3. `Pulse-Persona-dev` — Personas table, Step Functions workflow, Subscription API
4. `Pulse-Intelligence-dev` — Correlator, Predictor, Alarms
5. `Pulse-Analytics-dev` — S3 audit bucket, Athena, Analytics DDB table
6. `Pulse-Learning-dev` — Feedback processor, NRS calculator, Suppression scheduler

## Post-Deployment Setup

### 1. Verify SES Domain

```bash
aws ses verify-domain-identity --domain pulse-dev.example.com --region ap-southeast-1
```

Add the returned DNS records to your domain. For dev/testing, verify individual email addresses:

```bash
aws ses verify-email-identity --email-address noreply@pulse-dev.example.com
aws ses verify-email-identity --email-address ciso@example.com
```

### 2. Get API Key

```bash
# Get API Key ID from stack output
aws cloudformation describe-stacks --stack-name Pulse-Ingestion-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ApiKeyId'].OutputValue" --output text

# Get the actual key value
aws apigateway get-api-key --api-key <key-id> --include-value --query 'value' --output text
```

### 3. Test the Publish API

```bash
API_URL=$(aws cloudformation describe-stacks --stack-name Pulse-Ingestion-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)

curl -X POST "${API_URL}v1/signals" \
  -H "x-api-key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "custom.test",
    "signal_type": "incident",
    "severity": {"level": "medium", "score": 50},
    "content": {"title": "Test signal from deployment verification"}
  }'
```

### 4. Configure Webhook Secrets (Production)

Store secrets in Parameter Store:

```bash
aws ssm put-parameter --name /pulse/dev/pagerduty-secret --value "your-pd-secret" --type SecureString
aws ssm put-parameter --name /pulse/dev/datadog-api-key --value "your-dd-key" --type SecureString
aws ssm put-parameter --name /pulse/dev/servicenow-user --value "snow_user" --type SecureString
aws ssm put-parameter --name /pulse/dev/servicenow-pass --value "snow_pass" --type SecureString
```

Then update Lambda environment variables to reference these parameters.

## Cross-Account Setup (Optional)

Deploy the OrgSetupStack in the management account:

```bash
cd infra
npx cdk deploy Pulse-OrgSetup-dev \
  --context stage=dev \
  --context pulseAccountId=123456789012 \
  --context organizationId=o-abc123
```

For member accounts, deploy forwarding rules via StackSets or manually.

## Production Deployment

```bash
cd infra
npx cdk deploy --all --context stage=prod --require-approval broadening
```

Production differences:
- SES must be out of sandbox (request production access)
- Set real webhook secrets via Parameter Store
- Configure CloudWatch alarm SNS topic subscriptions (email/PagerDuty)
- Enable DynamoDB point-in-time recovery
- Set S3 bucket replication for DR

## Useful Commands

```bash
# Synthesize CloudFormation templates (dry run)
npx cdk synth --context stage=dev

# Diff against deployed stacks
npx cdk diff --all --context stage=dev

# Destroy all stacks (CAREFUL)
npx cdk destroy --all --context stage=dev

# Run tests
cd .. && PYTHONPATH=src:. python3 -m pytest tests/ -v
```
