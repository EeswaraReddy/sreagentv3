---
name: SRE Incident Handler Agent
description: Multi-agent incident handler deployed on AgentCore Runtime with MCP Gateway tools
---

# SRE Incident Handler Agent

## What It Does

Automated incident response agent for data platform SRE teams. Receives incidents from ServiceNow, classifies them, investigates root causes, executes remediation, and stores Root Cause Analysis documents.

## Architecture

```
EventBridge (5min) → Poller Lambda → Orchestrator Lambda
                                       │
                                       ▼
                           Strands Agent (Claude Sonnet)
                           ├── classify_incident       → Intent Classifier Agent
                           ├── investigate_incident     → Investigator Agent + MCP tools
                           ├── evaluate_before_action   → Deterministic gate
                           ├── execute_remediation      → Action Agent + MCP tools
                           ├── apply_policy_decision    → Policy Engine
                           ├── evaluate_before_close    → Deterministic gate
                           └── build_rca_document       → RCA builder
                                       │
                                       ▼ (via MCPClient)
                           AgentCore Gateway (IAM SigV4)
                           ├── get_emr_logs          → Lambda
                           ├── get_glue_logs         → Lambda
                           ├── get_mwaa_logs         → Lambda
                           ├── get_cloudwatch_alarm  → Lambda
                           ├── get_s3_logs           → Lambda
                           ├── verify_source_data    → Lambda
                           ├── get_athena_query      → Lambda
                           ├── retry_emr             → Lambda
                           ├── retry_glue_job        → Lambda
                           ├── retry_airflow_dag     → Lambda
                           ├── retry_athena_query    → Lambda
                           ├── retry_kafka           → Lambda
                           └── update_servicenow_ticket → Lambda
```

## Trigger

**EventBridge** polls ServiceNow every 5 minutes via the Poller Lambda. Each new/updated incident is sent to the Orchestrator Lambda.

**Manual invoke**:
```bash
aws lambda invoke --function-name incident-orchestrator \
  --payload '{"incident":{"sys_id":"INC001","short_description":"Glue job failed"}}' \
  response.json
```

## API Contract

### Input (event payload)
```json
{
  "incident": {
    "sys_id": "string (ServiceNow sys_id)",
    "short_description": "string",
    "description": "string (optional)",
    "category": "string (optional)",
    "subcategory": "string (optional)"
  }
}
```

### Output
```json
{
  "incident_id": "sys_id",
  "intent": "glue_etl_failure",
  "confidence": 0.92,
  "decision": "auto_close | auto_retry | escalate | human_review",
  "score": 0.85,
  "reasoning": "string",
  "rca_uri": "s3://bucket/rca/...",
  "actions_taken": [{"action": "retry_glue_job", "success": true}],
  "processing_time_ms": 12345,
  "orchestration_mode": "hybrid_agent_as_tool"
}
```

## MCP Tools (via AgentCore Gateway)

All tools are Lambda functions invoked through AgentCore Gateway. The agent NEVER calls AWS services directly.

| Tool | Type | Lambda | Purpose |
|------|------|--------|---------|
| `get_emr_logs` | Investigation | `incident-handler-get-emr-logs` | Fetch EMR cluster/step logs |
| `get_glue_logs` | Investigation | `incident-handler-get-glue-logs` | Fetch Glue job run logs |
| `get_mwaa_logs` | Investigation | `incident-handler-get-mwaa-logs` | Fetch Airflow DAG/task logs |
| `get_cloudwatch_alarm` | Investigation | `incident-handler-get-cloudwatch-alarm` | Fetch alarm state & history |
| `get_s3_logs` | Investigation | `incident-handler-get-s3-logs` | Fetch S3 access logs |
| `verify_source_data` | Investigation | `incident-handler-verify-source-data` | Check data availability |
| `get_athena_query` | Investigation | `incident-handler-get-athena-query` | Fetch query execution details |
| `retry_emr` | Remediation | `incident-handler-retry-emr` | Retry failed EMR step |
| `retry_glue_job` | Remediation | `incident-handler-retry-glue-job` | Retry failed Glue job |
| `retry_airflow_dag` | Remediation | `incident-handler-retry-airflow-dag` | Trigger Airflow DAG run |
| `retry_athena_query` | Remediation | `incident-handler-retry-athena-query` | Re-execute Athena query |
| `retry_kafka` | Remediation | `incident-handler-retry-kafka` | Retry Kafka event processing |
| `update_servicenow_ticket` | Integration | `incident-handler-update-servicenow-ticket` | Update/close ServiceNow ticket |

## Intent Taxonomy

| Intent | Tools Used | Policy |
|--------|-----------|--------|
| `dag_failure` | get_mwaa_logs, retry_airflow_dag | Normal |
| `dag_alarm` | get_mwaa_logs, get_cloudwatch_alarm | Normal |
| `mwaa_failure` | get_mwaa_logs, retry_airflow_dag | Normal |
| `glue_etl_failure` | get_glue_logs, retry_glue_job | Normal |
| `athena_failure` | get_athena_query, retry_athena_query | Normal |
| `emr_failure` | get_emr_logs, retry_emr | Normal |
| `kafka_events_failed` | retry_kafka | **Override → human_review** |
| `data_missing` | verify_source_data, get_s3_logs | Normal |
| `source_zero_data` | verify_source_data, get_s3_logs | Normal |
| `data_not_available` | verify_source_data, get_s3_logs | Normal |
| `batch_auto_recovery_failed` | get_cloudwatch_alarm | Normal |
| `access_denied` | — (skip investigation) | **Override → escalate** |

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GATEWAY_ENDPOINT` | Yes (prod) | `""` | AgentCore Gateway MCP endpoint |
| `GATEWAY_REGION` | No | `us-east-1` | AWS region for SigV4 signing |
| `BEDROCK_MODEL_ID` | No | `us.anthropic.claude-sonnet-4-20250514` | LLM model |
| `RCA_BUCKET` | Yes (prod) | `""` | S3 bucket for RCA docs |
| `LOG_LEVEL` | No | `INFO` | Logging level |

## Deployment

```bash
# 1. Install deps
pip install -r requirements.txt
cd cdk && pip install -r requirements.txt && cd ..

# 2. Deploy all Lambdas + infra
./deploy.sh

# 3. Create AgentCore Gateway (AWS Console or CLI)
# Point Gateway targets to the tool Lambda ARNs from CDK outputs

# 4. Set GATEWAY_ENDPOINT on orchestrator Lambda
aws lambda update-function-configuration \
  --function-name incident-orchestrator \
  --environment "Variables={GATEWAY_ENDPOINT=https://your-gateway.execute-api.region.amazonaws.com}"
```

## Local Testing (no AWS)

```bash
# Mock tests — no credentials needed
python test_mock_orchestrator.py

# Syntax check
python -m py_compile agents/gateway_client.py
python -m py_compile agents/orchestrator.py
```
