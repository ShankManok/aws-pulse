"""Strict persona and subscription contracts shared by management and routing."""
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator

CHANNELS = ('email', 'slack')


class Member(BaseModel):
    model_config = ConfigDict(extra='forbid')
    principalId: str = Field(min_length=1, max_length=256)
    channels: list[Literal['email', 'slack']] = Field(min_length=1, max_length=6)


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid')
    channels: list[Literal['email', 'slack']] = Field(default_factory=lambda: ['email'], min_length=1)
    cadence: Literal['realtime'] = 'realtime'
    quietHours: None = None
    escalationAfterMinutes: int = Field(default=30, ge=1, le=10080)


class Persona(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    roleTemplate: Literal['ciso', 'soc_analyst', 'cloud_ops', 'sre', 'finops', 'compliance', 'cto', 'account_owner']
    members: list[Member] = Field(min_length=1, max_length=100)
    languageLevel: Literal['executive', 'technical_summary', 'detailed_technical', 'business_data', 'formal_regulatory'] = 'technical_summary'
    deliveryPreferences: Preferences = Field(default_factory=Preferences)
    accountIds: list[str] = Field(default_factory=list)


class SubscriptionFilter(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sources: list[str] | None = None
    severity_min: Literal['critical', 'high', 'medium', 'low', 'informational'] | None = None
    regions: list[str] | None = None
    tags: dict[str, str] | None = None
    signal_types: list[Literal['incident', 'finding', 'recommendation', 'prediction', 'lifecycle']] | None = None
    keywords: list[str] | None = None
    account_ids: list[str] | None = None

    @model_validator(mode='after')
    def constrained(self):
        values = self.model_dump(exclude_none=True)
        if not values or any(not value for value in values.values()):
            raise ValueError('A nonempty filter is required')
        for value in values.values():
            if isinstance(value, list) and (len(value) > 20 or any(not s.strip() or len(s) > 256 for s in value)):
                raise ValueError('Invalid filter values')
        return self
