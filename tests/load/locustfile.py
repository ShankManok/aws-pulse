"""Locust load test for the AWS Pulse Publish API.

Scenarios:
  - Burst: 1000 signals in 10 seconds
  - Sustained: 100 signals/sec for 5 minutes
  - Mixed severity distribution (critical 5%, high 20%, medium 40%, low 25%, info 10%)

Run:
  locust -f tests/load/locustfile.py --host https://<api-id>.execute-api.<region>.amazonaws.com/<stage>

Headless (CI):
  locust -f tests/load/locustfile.py --headless -u 100 -r 20 --run-time 5m \
    --host https://<api-id>.execute-api.<region>.amazonaws.com/<stage>
"""
import json
import random
import time
import uuid

from locust import HttpUser, task, between, events


# --- Signal generators ---

SEVERITIES = [
    ("critical", 90, 0.05),
    ("high", 75, 0.20),
    ("medium", 50, 0.40),
    ("low", 30, 0.25),
    ("informational", 10, 0.10),
]

SOURCES = [
    "aws.cloudwatch",
    "aws.securityhub",
    "aws.health",
    "custom.myapp",
    "pagerduty",
    "datadog",
]

SIGNAL_TYPES = ["incident", "finding", "recommendation", "prediction", "lifecycle"]

REGIONS = ["ap-southeast-1", "us-east-1", "eu-west-1", "us-west-2"]


def _weighted_severity():
    """Pick a severity level based on realistic distribution."""
    r = random.random()
    cumulative = 0.0
    for level, score, weight in SEVERITIES:
        cumulative += weight
        if r <= cumulative:
            return level, score
    return "medium", 50


def _generate_signal():
    """Generate a random but realistic signal payload."""
    severity_level, severity_score = _weighted_severity()
    source = random.choice(SOURCES)
    signal_type = random.choice(SIGNAL_TYPES)
    region = random.choice(REGIONS)

    return {
        "source": source,
        "signal_type": signal_type,
        "severity": {
            "level": severity_level,
            "score": severity_score,
            "blast_radius": {
                "accounts": [f"{random.randint(100000000000, 999999999999)}"],
                "resources": [f"arn:aws:ec2:{region}:123456789012:instance/i-{uuid.uuid4().hex[:8]}"],
                "services": [random.choice(["ec2", "rds", "s3", "lambda", "ecs"])],
            },
        },
        "content": {
            "title": f"Load test signal: {source} {severity_level} {signal_type}",
            "raw_detail": f"Automated load test signal generated at {time.time()}. "
                          f"This verifies the Publish API can handle sustained throughput.",
            "recommended_actions": [],
        },
        "context": {
            "account_id": "123456789012",
            "region": region,
            "resource_arns": [f"arn:aws:ec2:{region}:123456789012:instance/i-{uuid.uuid4().hex[:8]}"],
            "tags": {"Environment": random.choice(["production", "staging", "dev"]), "LoadTest": "true"},
        },
        "audience_hint": {
            "personas": ["sre"],
            "escalation_chain": [],
            "sla_acknowledge_minutes": 30,
        },
    }


# --- Locust User ---

class PulsePublishUser(HttpUser):
    """Simulates a signal producer publishing to the Pulse API."""

    wait_time = between(0.01, 0.1)  # 10-100ms between requests per user

    def on_start(self):
        """Set API key header (get from environment or use test key)."""
        import os
        self.api_key = os.environ.get("PULSE_API_KEY", "test-api-key")
        self.headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }

    @task(8)
    def publish_signal(self):
        """Publish a random signal (main workload)."""
        payload = _generate_signal()
        with self.client.post(
            "/v1/signals",
            json=payload,
            headers=self.headers,
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                response.success()
            elif response.status_code == 429:
                response.failure("Rate limited (429)")
            else:
                response.failure(f"Unexpected status: {response.status_code}")

    @task(1)
    def publish_critical_signal(self):
        """Publish a critical signal (higher priority path)."""
        payload = _generate_signal()
        payload["severity"] = {"level": "critical", "score": 95, "blast_radius": payload["severity"]["blast_radius"]}
        payload["signal_type"] = "incident"
        payload["audience_hint"]["personas"] = ["ciso", "sre", "cto"]

        with self.client.post(
            "/v1/signals",
            json=payload,
            headers=self.headers,
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                response.success()
            elif response.status_code == 429:
                response.failure("Rate limited (429)")
            else:
                response.failure(f"Unexpected status: {response.status_code}")

    @task(1)
    def publish_burst(self):
        """Simulate a burst of 10 signals in rapid succession."""
        for _ in range(10):
            payload = _generate_signal()
            self.client.post(
                "/v1/signals",
                json=payload,
                headers=self.headers,
                name="/v1/signals [burst]",
            )


# --- Custom event hooks for reporting ---

@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Print summary statistics at test end."""
    stats = environment.runner.stats
    total = stats.total
    print(f"\n{'='*60}")
    print(f"LOAD TEST SUMMARY")
    print(f"{'='*60}")
    print(f"Total requests: {total.num_requests}")
    print(f"Failures: {total.num_failures}")
    print(f"Avg response time: {total.avg_response_time:.0f}ms")
    print(f"P95 response time: {total.get_response_time_percentile(0.95):.0f}ms")
    print(f"P99 response time: {total.get_response_time_percentile(0.99):.0f}ms")
    print(f"Requests/sec: {total.current_rps:.1f}")
    print(f"{'='*60}")

    # Validate targets
    p99 = total.get_response_time_percentile(0.99)
    failures = total.num_failures
    if p99 > 500:
        print(f"WARNING: P99 latency {p99:.0f}ms exceeds 500ms target")
    if failures > 0:
        fail_rate = failures / max(total.num_requests, 1) * 100
        print(f"WARNING: {failures} failures ({fail_rate:.1f}% failure rate)")
    if p99 <= 500 and failures == 0:
        print("ALL TARGETS MET")
