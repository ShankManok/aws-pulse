"""AWS Pulse Python SDK - Client for the Pulse Publish API."""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import botocore.auth
import botocore.credentials
import botocore.session
import requests
from pydantic import BaseModel, Field


# --- Response Models ---

class PublishSignalResponse(BaseModel):
    signal_id: str = Field(alias="signalId")
    status: str


class PersonaResponse(BaseModel):
    persona_id: str = Field(alias="personaId")


class SignalResponse(BaseModel):
    signal_id: str = Field(alias="signalId", default="")
    source: str = ""
    signal_type: str = Field(alias="signalType", default="")
    severity: dict = Field(default_factory=dict)
    content: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)
    status: str = ""
    ingested_at: str = Field(alias="ingestedAt", default="")


class DeliveryRecord(BaseModel):
    delivery_id: str = Field(alias="deliveryId", default="")
    signal_id: str = Field(alias="signalId", default="")
    persona_id: str = Field(alias="personaId", default="")
    recipient_id: str = Field(alias="recipientId", default="")
    channel: str = ""
    delivered_at: str = Field(alias="deliveredAt", default="")
    acknowledged_at: Optional[str] = Field(alias="acknowledgedAt", default=None)
    escalated: bool = False
    feedback: Optional[str] = None


class ListDeliveriesResponse(BaseModel):
    deliveries: list[DeliveryRecord] = Field(default_factory=list)
    next_token: Optional[str] = Field(alias="nextToken", default=None)


# --- Exceptions ---

class PulseError(Exception):
    """Base exception for Pulse SDK errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class PulseValidationError(PulseError):
    """Raised for 400 Bad Request responses."""
    pass


class PulseNotFoundError(PulseError):
    """Raised for 404 Not Found responses."""
    pass


class PulseThrottlingError(PulseError):
    """Raised for 429 Too Many Requests responses."""
    pass


# --- SigV4 Request Signing ---

class _SigV4Auth:
    """Signs HTTP requests with AWS SigV4 using botocore credentials."""

    def __init__(self, region: str, service: str = "execute-api"):
        self._region = region
        self._service = service
        session = botocore.session.get_session()
        self._credentials = session.get_credentials().get_frozen_credentials()

    def sign_request(self, method: str, url: str, headers: dict, body: str = "") -> dict:
        """Add SigV4 authorization headers to the request."""
        from botocore.awsrequest import AWSRequest

        request = AWSRequest(method=method, url=url, headers=headers, data=body)
        signer = botocore.auth.SigV4Auth(self._credentials, self._service, self._region)
        signer.add_auth(request)
        return dict(request.headers)


# --- Main Client ---

class PulseClient:
    """Client for the AWS Pulse API.

    Authenticates requests using SigV4 signing with the caller's AWS credentials.

    Args:
        endpoint_url: Base URL of the Pulse API (e.g., "https://abc123.execute-api.ap-southeast-1.amazonaws.com/dev")
        region: AWS region where Pulse is deployed
        max_retries: Maximum number of retries on transient failures
        timeout: HTTP request timeout in seconds
    """

    def __init__(
        self,
        endpoint_url: str,
        region: str = "ap-southeast-1",
        max_retries: int = 3,
        timeout: int = 30,
    ):
        if not endpoint_url:
            raise ValueError("endpoint_url is required")

        self._endpoint = endpoint_url.rstrip("/")
        self._region = region
        self._max_retries = max_retries
        self._timeout = timeout
        self._auth = _SigV4Auth(region=region)
        self._session = requests.Session()

    # --- Public Methods ---

    def publish_signal(
        self,
        source: str,
        signal_type: str,
        severity: dict,
        content: dict,
        context: Optional[dict] = None,
        audience_hint: Optional[dict] = None,
        correlation: Optional[dict] = None,
    ) -> PublishSignalResponse:
        """Publish a signal to AWS Pulse.

        Args:
            source: Signal source identifier (e.g., "custom.myapp", "aws.devops-agent")
            signal_type: One of: incident, finding, recommendation, prediction, lifecycle
            severity: {"level": "high", "score": 75, "blast_radius": {...}}
            content: {"title": "...", "raw_detail": "...", "recommended_actions": [...]}
            context: {"account_id": "...", "region": "...", "resource_arns": [...]}
            audience_hint: {"personas": [...], "escalation_chain": [...], "sla_acknowledge_minutes": N}
            correlation: {"correlation_id": "...", "time_window_seconds": N}

        Returns:
            PublishSignalResponse with signal_id and status

        Raises:
            PulseValidationError: If request body is invalid
            PulseThrottlingError: If rate limited
            PulseError: For other API errors
        """
        payload: dict[str, Any] = {
            "source": source,
            "signal_type": signal_type,
            "severity": severity,
            "content": content,
        }
        if context:
            payload["context"] = context
        if audience_hint:
            payload["audience_hint"] = audience_hint
        if correlation:
            payload["correlation"] = correlation

        response = self._request("POST", "/v1/signals", body=payload)
        return PublishSignalResponse.model_validate(response)

    def get_signal(self, signal_id: str) -> SignalResponse:
        """Retrieve a signal by ID.

        Args:
            signal_id: The signal's unique identifier (ULID)

        Returns:
            SignalResponse with full signal data

        Raises:
            PulseNotFoundError: If signal doesn't exist
        """
        response = self._request("GET", f"/v1/signals/{signal_id}")
        return SignalResponse.model_validate(response)

    def create_persona(
        self,
        name: str,
        role_template: str,
        members: list[dict],
        language_level: str = "technical_summary",
        delivery_preferences: Optional[dict] = None,
        org_id: str = "default",
    ) -> PersonaResponse:
        """Create a new persona.

        Args:
            name: Display name for the persona (e.g., "CISO")
            role_template: One of: ciso, soc_analyst, cloud_ops, sre, finops, compliance, cto, account_owner
            members: List of {"principalId": "email@...", "channels": ["email", "slack"]}
            language_level: One of: executive, technical_summary, detailed_technical, business_data, formal_regulatory
            delivery_preferences: {"channels": [...], "cadence": "...", "escalationAfterMinutes": N}
            org_id: Organization identifier for multi-tenant isolation

        Returns:
            PersonaResponse with the new persona_id
        """
        payload: dict[str, Any] = {
            "name": name,
            "roleTemplate": role_template,
            "members": members,
            "languageLevel": language_level,
            "orgId": org_id,
        }
        if delivery_preferences:
            payload["deliveryPreferences"] = delivery_preferences

        response = self._request("POST", "/v1/personas", body=payload)
        return PersonaResponse.model_validate(response)

    def update_persona(self, persona_id: str, **updates) -> PersonaResponse:
        """Update an existing persona's configuration.

        Args:
            persona_id: The persona to update
            **updates: Fields to update (members, deliveryPreferences, subscriptions, etc.)

        Returns:
            PersonaResponse confirming the update
        """
        response = self._request("PUT", f"/v1/personas/{persona_id}", body=updates)
        return PersonaResponse.model_validate(response)

    def list_deliveries(
        self,
        signal_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        limit: int = 50,
        next_token: Optional[str] = None,
    ) -> ListDeliveriesResponse:
        """List delivery records with optional filtering.

        Args:
            signal_id: Filter by signal ID
            persona_id: Filter by persona ID
            limit: Maximum records to return (default 50, max 100)
            next_token: Pagination token from previous response

        Returns:
            ListDeliveriesResponse with delivery records and optional next_token
        """
        params: dict[str, str] = {"limit": str(min(limit, 100))}
        if signal_id:
            params["signalId"] = signal_id
        if persona_id:
            params["personaId"] = persona_id
        if next_token:
            params["nextToken"] = next_token

        response = self._request("GET", "/v1/deliveries", params=params)
        return ListDeliveriesResponse.model_validate(response)

    def submit_feedback(self, delivery_id: str, feedback: str) -> dict:
        """Submit feedback on a delivered notification.

        Args:
            delivery_id: The delivery record ID
            feedback: One of: "useful", "noise", "escalate"

        Returns:
            Confirmation dict with deliveryId and action
        """
        if feedback not in ("useful", "noise", "escalate"):
            raise PulseValidationError(f"Invalid feedback: {feedback}. Must be useful, noise, or escalate")

        return self._request("POST", "/v1/feedback", body={
            "deliveryId": delivery_id,
            "feedback": feedback,
        })

    # --- Private Methods ---

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """Make an authenticated API request with retries."""
        url = f"{self._endpoint}{path}"
        headers = {"Content-Type": "application/json"}
        body_str = json.dumps(body) if body else ""

        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())

        last_error: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                signed_headers = self._auth.sign_request(method, url, headers, body_str)
                response = self._session.request(
                    method=method,
                    url=url,
                    headers=signed_headers,
                    data=body_str if body else None,
                    timeout=self._timeout,
                )

                if response.status_code == 429:
                    # Retry on throttling with exponential backoff
                    if attempt < self._max_retries:
                        wait = 2 ** attempt
                        time.sleep(wait)
                        continue
                    raise PulseThrottlingError(
                        "Rate limited",
                        status_code=429,
                        response_body=response.text,
                    )

                if response.status_code >= 500:
                    # Retry on server errors
                    if attempt < self._max_retries:
                        wait = 2 ** attempt
                        time.sleep(wait)
                        continue

                return self._handle_response(response)

            except (requests.ConnectionError, requests.Timeout) as e:
                last_error = e
                if attempt < self._max_retries:
                    wait = 2 ** attempt
                    time.sleep(wait)
                    continue
                raise PulseError(f"Connection failed after {self._max_retries} retries: {e}")

        raise PulseError(f"Request failed: {last_error}")

    def _handle_response(self, response: requests.Response) -> dict:
        """Parse response and raise appropriate exceptions."""
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text}

        if response.status_code in (200, 201):
            return data

        if response.status_code == 400:
            raise PulseValidationError(
                data.get("error", "Validation error"),
                status_code=400,
                response_body=data,
            )

        if response.status_code == 404:
            raise PulseNotFoundError(
                data.get("error", "Not found"),
                status_code=404,
                response_body=data,
            )

        if response.status_code == 429:
            raise PulseThrottlingError(
                "Rate limited",
                status_code=429,
                response_body=data,
            )

        raise PulseError(
            data.get("error", f"API error: {response.status_code}"),
            status_code=response.status_code,
            response_body=data,
        )
