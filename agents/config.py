"""Configuration for incident handler agents."""
import os
from typing import Any

# Model configuration
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514")

# Gateway configuration
GATEWAY_ENDPOINT = os.environ.get("GATEWAY_ENDPOINT", "")
GATEWAY_REGION = os.environ.get("GATEWAY_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

# S3 RCA configuration
RCA_BUCKET = os.environ.get("RCA_BUCKET", "")
RCA_PREFIX = os.environ.get("RCA_PREFIX", "rca/")

# CloudWatch metrics namespace
METRICS_NAMESPACE = "IncidentHandler"

# Intent taxonomy
INTENT_TAXONOMY = [
    "dag_failure",
    "dag_alarm",
    "mwaa_failure",
    "glue_etl_failure",
    "athena_failure",
    "emr_failure",
    "kafka_events_failed",
    "data_missing",
    "source_zero_data",
    "data_not_available",
    "batch_auto_recovery_failed",
    "access_denied",
    "unknown"
]

# Policy overrides - always apply these decisions for specific intents
POLICY_OVERRIDES = {
    "access_denied": "escalate",
    "kafka_events_failed": "human_review",
}

# Decision thresholds
POLICY_THRESHOLDS = {
    "auto_close": 0.8,    # High confidence + successful action
    "auto_retry": 0.6,    # Medium confidence + action possible
    "escalate": 0.4,      # Low confidence or complex issue
    "human_review": 0.0,  # Fallback for everything else
}

# Intelligent routing: intents that don't need deep investigation
SKIP_INVESTIGATION_INTENTS = {
    "access_denied",      # Access requests → escalate directly
}

# Fast-track: intents that skip both investigation AND action
FAST_TRACK_INTENTS = {
    "access_denied",      # No technical diagnosis needed
}

# Evaluation gate thresholds — must pass BEFORE updating/closing incidents
EVALUATION_THRESHOLDS = {
    "min_confidence_for_auto_action": 0.6,   # Min intent confidence to allow remediation
    "min_evidence_for_auto_close": 0.7,      # Min evidence score to allow auto-close
    "min_combined_for_auto_action": 0.5,     # Min combined score to allow any automated action
    "require_policy_approval": True,          # Policy check mandatory before close
}

# Intent to tool mapping - helps with semantic search
INTENT_TOOL_MAPPING = {
    "emr_failure": ["get_emr_logs", "retry_emr"],
    "glue_etl_failure": ["get_glue_logs", "retry_glue_job"],
    "mwaa_failure": ["get_mwaa_logs", "retry_airflow_dag"],
    "dag_failure": ["get_mwaa_logs", "retry_airflow_dag"],
    "dag_alarm": ["get_mwaa_logs", "get_cloudwatch_alarm"],
    "athena_failure": ["get_athena_query", "retry_athena_query"],
    "kafka_events_failed": ["retry_kafka"],
    "data_missing": ["verify_source_data", "get_s3_logs"],
    "source_zero_data": ["verify_source_data", "get_s3_logs"],
    "data_not_available": ["verify_source_data", "get_s3_logs"],
    "access_denied": ["get_s3_logs", "get_cloudwatch_alarm"],
    "batch_auto_recovery_failed": ["get_cloudwatch_alarm"],
    "unknown": [],
}

# Log level
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
