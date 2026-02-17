"""Main Orchestrator - BedrockAgentCoreApp handler entrypoint."""
import json
import logging
import os
import boto3
from datetime import datetime
from typing import Any

# Configure logging
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# Import orchestrator (hybrid agent-as-tool pattern)
from .orchestrator import OrchestratorAgent, create_orchestrator, orchestrate_incident
from .schemas import validate_output
from .config import RCA_BUCKET, RCA_PREFIX, METRICS_NAMESPACE
from .gateway_client import GatewayToolProvider

# Initialize AWS clients
s3 = boto3.client("s3")
cloudwatch = boto3.client("cloudwatch")



# BedrockAgentCoreApp decorator setup
try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
    app = BedrockAgentCoreApp()
except ImportError:
    try:
        # Fallback: try strands-agents package
        from strands.agent.bedrock import BedrockAgentCoreApp
        app = BedrockAgentCoreApp()
    except ImportError:
        # Fallback for local testing without AgentCore SDK
        logger.warning("BedrockAgentCoreApp not available, using mock decorator for local testing")
        class MockApp:
            def handler(self, func):
                return func
        app = MockApp()


def emit_metric(metric_name: str, value: float = 1.0, dimensions: dict = None, unit: str = "Count"):
    """Emit a CloudWatch metric."""
    try:
        metric_data = {
            "MetricName": metric_name,
            "Value": value,
            "Unit": unit,
            "Timestamp": datetime.utcnow()
        }
        if dimensions:
            metric_data["Dimensions"] = [
                {"Name": k, "Value": v} for k, v in dimensions.items()
            ]
        
        cloudwatch.put_metric_data(
            Namespace=METRICS_NAMESPACE,
            MetricData=[metric_data]
        )
    except Exception as e:
        logger.warning(f"Failed to emit metric {metric_name}: {e}")


def store_rca_to_s3(sys_id: str, rca: dict) -> str:
    """Store RCA document to S3.
    
    Args:
        sys_id: Incident sys_id
        rca: RCA document
        
    Returns:
        S3 URI of stored RCA
    """
    if not RCA_BUCKET:
        logger.warning("RCA_BUCKET not configured, skipping S3 storage")
        return ""
    
    try:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        key = f"{RCA_PREFIX}{sys_id}/{timestamp}_rca.json"
        
        s3.put_object(
            Bucket=RCA_BUCKET,
            Key=key,
            Body=json.dumps(rca, indent=2, default=str),
            ContentType="application/json",
            Metadata={
                "incident-id": sys_id,
                "generated-by": "incident-handler-orchestrator",
                "decision": rca.get("decision", {}).get("outcome", "unknown")
            }
        )
        
        s3_uri = f"s3://{RCA_BUCKET}/{key}"
        logger.info(f"RCA stored at {s3_uri}")
        return s3_uri
        
    except Exception as e:
        logger.error(f"Failed to store RCA: {e}")
        return ""


@app.handler
def handler(event: dict, context: dict) -> dict:
    """Main orchestrator handler for Bedrock AgentCore Runtime.
    
    This is the entrypoint for the multi-agent incident handler.
    Uses the hybrid agent-as-tool orchestration pattern:
    - LLM decides which agents to call (intelligent routing)
    - Deterministic guardrails enforce policy and evaluation gates
    - Mandatory evaluation before any incident update/closure
    
    Args:
        event: Contains incident payload from ServiceNow
        context: AgentCore runtime context
        
    Returns:
        Final decision and RCA
    """
    start_time = datetime.utcnow()
    
    # Extract incident from event
    incident = event.get("incident", event)
    sys_id = incident.get("sys_id", "unknown")
    
    logger.info(f"Processing incident {sys_id}: {incident.get('short_description', 'N/A')}")
    emit_metric("Invocations", dimensions={"Agent": "Orchestrator"})
    
    # Connect to AgentCore Gateway for MCP tools
    # Tools are Lambda functions invoked via Gateway (IAM role auth)
    gateway = GatewayToolProvider()
    
    try:
        # ============ FETCH MCP TOOLS FROM GATEWAY ============
        # Gateway exposes Lambda functions as MCP tools:
        #   Agent → MCPClient → AgentCore Gateway (IAM) → Lambda
        mcp_tools = gateway.start()
        logger.info(f"Gateway tools available: {len(mcp_tools)}")
        
        # ============ HYBRID ORCHESTRATION ============
        # The OrchestratorAgent uses LLM-driven routing with agents-as-tools.
        # Evaluation gates and policy overrides are enforced deterministically.
        
        # Create orchestrator with Gateway MCP tools
        orchestrator = create_orchestrator(mcp_tools=mcp_tools)
        rca = orchestrator.orchestrate(incident)
        
        # ============ POST-ORCHESTRATION: DETERMINISTIC STEPS ============
        
        # Extract key results for metrics and response
        intent = rca.get("classification", {}).get("intent", "unknown")
        confidence = rca.get("classification", {}).get("confidence", 0.0)
        decision = rca.get("decision", {}).get("outcome", "human_review")
        score = rca.get("decision", {}).get("score", 0.0)
        reasoning = rca.get("decision", {}).get("reasoning", "")
        
        # Emit metrics
        emit_metric("Classification", dimensions={"Intent": intent})
        emit_metric("Confidence", value=confidence, unit="None")
        emit_metric("Decision", dimensions={"Outcome": decision})
        
        if confidence < 0.3:
            emit_metric("LowConfidence", dimensions={"Intent": intent})
        
        if rca.get("guardrails"):
            for guardrail in rca["guardrails"]:
                emit_metric("GuardrailEnforced", dimensions={
                    "Type": guardrail.get("type", "unknown")
                })
        
        # Store RCA to S3
        rca_uri = store_rca_to_s3(sys_id, rca)
        
        # Update ServiceNow (if credentials available)
        servicenow_creds = event.get("servicenow_credentials") or (
            context.get("servicenow_credentials") if context else None
        )
        servicenow_update = None
        
        if servicenow_creds and mcp_tools:
            logger.info("Updating ServiceNow ticket")
            sn_tool = next((t for t in mcp_tools if "servicenow" in t.__name__.lower()), None)
            if sn_tool:
                try:
                    sn_update = sn_tool({
                        "sys_id": sys_id,
                        "status": _decision_to_status(decision),
                        "rca": rca,
                        "work_notes": f"Automated analysis complete. Decision: {decision}",
                    })
                    servicenow_update = sn_update
                except Exception as e:
                    logger.error(f"ServiceNow update failed: {e}")
                    servicenow_update = {"success": False, "error": str(e)}
        
        # ============ BUILD FINAL RESPONSE ============
        processing_time = int((datetime.utcnow() - start_time).total_seconds() * 1000)
        emit_metric("Latency", value=processing_time,
                     dimensions={"Agent": "Orchestrator"}, unit="Milliseconds")
        
        final_response = {
            "incident_id": sys_id,
            "intent": intent,
            "confidence": confidence,
            "decision": decision,
            "score": score,
            "reasoning": reasoning,
            "rca_uri": rca_uri,
            "actions_taken": [],
            "processing_time_ms": processing_time,
            "orchestration_mode": "hybrid_agent_as_tool",
        }
        
        # Include action if one was taken
        action_taken = rca.get("remediation", {}).get("action_taken", "none")
        if action_taken != "none":
            final_response["actions_taken"] = [{
                "action": action_taken,
                "success": rca.get("remediation", {}).get("action_success", False),
            }]
        
        # Include guardrail info if any were triggered
        if rca.get("guardrails"):
            final_response["guardrails_triggered"] = rca["guardrails"]
        
        if servicenow_update:
            final_response["servicenow_update"] = servicenow_update
        
        # Validate final output
        is_valid, error = validate_output(final_response, "orchestrator")
        if not is_valid:
            logger.warning(f"Orchestrator output validation failed: {error}")
            emit_metric("Failure", dimensions={"Schema": "orchestrator"})
            final_response["validation_warning"] = error
        
        logger.info(f"Incident {sys_id} processed. Decision: {decision}")
        return final_response
        
    except Exception as e:
        logger.error(f"Orchestrator error for {sys_id}: {str(e)}", exc_info=True)
        emit_metric("Error", dimensions={"Agent": "Orchestrator"})
        
        return {
            "incident_id": sys_id,
            "intent": "unknown",
            "confidence": 0.0,
            "decision": "human_review",
            "score": 0.0,
            "reasoning": f"Processing error: {str(e)}",
            "error": str(e),
        }
    finally:
        # Always clean up the Gateway MCP session
        gateway.stop()


def _human_review_response(sys_id: str, reason: str, partial_results: dict) -> dict:
    """Build a human review response for validation failures."""
    return {
        "incident_id": sys_id,
        "intent": partial_results.get("stages", {}).get("intent", {}).get("intent", "unknown"),
        "confidence": 0.0,
        "decision": "human_review",
        "score": 0.0,
        "reasoning": reason,
        "partial_results": partial_results
    }


def _decision_to_status(decision: str) -> str:
    """Map policy decision to ServiceNow status."""
    mapping = {
        "auto_close": "resolved",
        "auto_retry": "in_progress",
        "escalate": "escalated",
        "human_review": "on_hold"
    }
    return mapping.get(decision, "in_progress")


# Synchronous wrapper for testing
def handler_sync(event: dict, context: dict = None) -> dict:
    """Synchronous handler for local testing."""
    return handler(event, context or {})


if __name__ == "__main__":
    # Test with sample incidents
    test_incidents = [
        {
            "incident": {
                "sys_id": "TEST123",
                "short_description": "Glue job 'etl-daily-load' failed with OutOfMemory error",
                "category": "Data Pipeline",
                "subcategory": "ETL",
            }
        },
        {
            "incident": {
                "sys_id": "TEST456",
                "short_description": "I need access to production table customer_data in Athena",
                "category": "Access Request",
                "subcategory": "Database",
            }
        },
    ]
    
    for event in test_incidents:
        print(f"\n{'=' * 80}")
        print(f"Testing: {event['incident']['short_description']}")
        print(f"{'=' * 80}")
        result = handler_sync(event)
        print(json.dumps(result, indent=2, default=str))
