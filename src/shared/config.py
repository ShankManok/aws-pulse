"""Environment configuration."""
import os


class Config:
    STAGE = os.environ.get("STAGE", "dev")
    SIGNAL_STREAM_NAME = os.environ.get("SIGNAL_STREAM_NAME", "")
    SIGNAL_TABLE_NAME = os.environ.get("SIGNAL_TABLE_NAME", "")
    PERSONA_TABLE_NAME = os.environ.get("PERSONA_TABLE_NAME", "")
    DELIVERY_TABLE_NAME = os.environ.get("DELIVERY_TABLE_NAME", "")
    CORRELATION_TABLE_NAME = os.environ.get("CORRELATION_TABLE_NAME", "")
    BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-20250514")
    ESCALATION_DEFAULT_MINUTES = int(os.environ.get("ESCALATION_DEFAULT_MINUTES", "15"))
