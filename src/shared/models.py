"""Pydantic data models for AWS Pulse."""
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
import ulid


class SignalType(str, Enum):
    INCIDENT = "incident"
    FINDING = "finding"
    RECOMMENDATION = "recommendation"
    PREDICTION = "prediction"
    LIFECYCLE = "lifecycle"


class SeverityLevel(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFORMATIONAL = "informational"


class SignalStatus(str, Enum):
    NEW = "new"
    CORRELATED = "correlated"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    SUPPRESSED = "suppressed"


class BlastRadius(BaseModel):
    accounts: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)


class Severity(BaseModel):
    level: SeverityLevel
    score: int = Field(ge=0, le=100, default=50)
    blast_radius: BlastRadius = Field(default_factory=BlastRadius)


class RecommendedAction(BaseModel):
    action: str
    label: str
    api_call: str = ""


class SignalContent(BaseModel):
    title: str
    raw_detail: str = ""
    structured_data: dict = Field(default_factory=dict)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)


class SignalContext(BaseModel):
    account_id: str = ""
    region: str = ""
    resource_arns: list[str] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    investigation_id: Optional[str] = None
    runbook_url: Optional[str] = None


class AudienceHint(BaseModel):
    personas: list[str] = Field(default_factory=list)
    escalation_chain: list[str] = Field(default_factory=list)
    sla_acknowledge_minutes: int = 30


class Correlation(BaseModel):
    correlation_id: Optional[str] = None
    time_window_seconds: int = 300


class SignalEvent(BaseModel):
    """Canonical signal event schema."""
    signal_id: str = Field(default_factory=lambda: str(ulid.new()))
    source: str
    signal_type: SignalType
    severity: Severity
    content: SignalContent
    context: SignalContext = Field(default_factory=SignalContext)
    audience_hint: AudienceHint = Field(default_factory=AudienceHint)
    correlation: Correlation = Field(default_factory=Correlation)
    status: SignalStatus = SignalStatus.NEW
    ingested_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    correlation_group_id: Optional[str] = None

    def to_dynamo(self) -> dict:
        """Serialize for DynamoDB."""
        return self.model_dump(mode="json", exclude_none=True)
