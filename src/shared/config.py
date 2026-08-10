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
    # Phase 2
    PERSONA_WORKFLOW_ARN = os.environ.get("PERSONA_WORKFLOW_ARN", "")
    SCHEDULER_GROUP_NAME = os.environ.get("SCHEDULER_GROUP_NAME", "pulse-escalations")
    SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
    ESCALATION_FUNCTION_ARN = os.environ.get("ESCALATION_FUNCTION_ARN", "")
    CHATBOT_SNS_TOPIC_ARN = os.environ.get("CHATBOT_SNS_TOPIC_ARN", "")
    CALLBACK_API_URL = os.environ.get("CALLBACK_API_URL", "")
    # Phase 3
    ANALYTICS_TABLE_NAME = os.environ.get("ANALYTICS_TABLE_NAME", "")
    AUDIT_BUCKET_NAME = os.environ.get("AUDIT_BUCKET_NAME", "")
    NOISE_THRESHOLD = int(os.environ.get("NOISE_THRESHOLD", "3"))
    WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "30"))
    # Phase 4
    PREDICTORS_TABLE_NAME = os.environ.get("PREDICTORS_TABLE_NAME", "")
