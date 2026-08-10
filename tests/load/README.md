# Pulse Load Testing

Load tests for the AWS Pulse Publish API using [Locust](https://locust.io/).

## Prerequisites

```bash
pip install locust
```

## Configuration

Set environment variables:

```bash
export PULSE_API_KEY="your-api-key-here"    # From CDK output: ApiKeyId
export PULSE_API_HOST="https://<api-id>.execute-api.ap-southeast-1.amazonaws.com/dev"
```

Get your API key value from AWS Console > API Gateway > API Keys, or via CLI:
```bash
aws apigateway get-api-key --api-key <ApiKeyId> --include-value --query 'value' --output text
```

## Running Tests

### Interactive (Web UI)

```bash
locust -f tests/load/locustfile.py --host $PULSE_API_HOST
```

Then open http://localhost:8089 to configure users and start the test.

### Headless (CLI / CI)

Sustained load (100 req/sec for 5 minutes):
```bash
locust -f tests/load/locustfile.py --headless \
  -u 100 -r 20 --run-time 5m \
  --host $PULSE_API_HOST
```

Burst test (high concurrency, short duration):
```bash
locust -f tests/load/locustfile.py --headless \
  -u 500 -r 100 --run-time 30s \
  --host $PULSE_API_HOST
```

Soak test (low rate, long duration):
```bash
locust -f tests/load/locustfile.py --headless \
  -u 20 -r 5 --run-time 30m \
  --host $PULSE_API_HOST
```

## Test Scenarios

| Scenario | Users | Spawn Rate | Duration | Target |
|----------|-------|-----------|----------|--------|
| Burst | 500 | 100/s | 30s | No 5xx, p99 < 500ms |
| Sustained | 100 | 20/s | 5m | 100 req/s, p99 < 500ms |
| Soak | 20 | 5/s | 30m | Zero failures, stable latency |

## Validation Targets

- API latency p99 < 500ms
- Zero 5xx errors under sustained load
- Rate limiting kicks in at 1000 req/s (429 responses expected above this)
- All signals appear in DynamoDB (verify with scan after test)

## Verifying Results

After a load test run, verify signals were persisted:

```bash
aws dynamodb scan \
  --table-name pulse-events-dev \
  --filter-expression "contains(content.title, :prefix)" \
  --expression-attribute-values '{":prefix": {"S": "Load test signal"}}' \
  --select COUNT
```

## CI Integration

Add to your CI pipeline:

```yaml
load-test:
  script:
    - pip install locust
    - locust -f tests/load/locustfile.py --headless -u 50 -r 10 --run-time 2m --host $PULSE_API_HOST --exit-code-on-error 1
```
