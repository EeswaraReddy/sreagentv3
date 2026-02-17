"""Investigator Agent - gathers evidence using MCP Gateway tools."""
import json
import logging

from strands import Agent
from strands.models import BedrockModel

from .config import MODEL_ID, INTENT_TOOL_MAPPING
from .schemas import parse_agent_response
from .prompts import INVESTIGATOR_PROMPT

logger = logging.getLogger(__name__)

# Lazy-loaded BedrockModel — avoids import failures without AWS creds
_bedrock_model = None

def _get_bedrock_model():
    global _bedrock_model
    if _bedrock_model is None:
        _bedrock_model = BedrockModel(model_id=MODEL_ID)
    return _bedrock_model


def create_investigator_agent(tools: list = None) -> Agent:
    """Create the investigator agent with MCP tools.
    
    Args:
        tools: List of MCP tools from Gateway
        
    Returns:
        Configured Agent instance
    """
    return Agent(
        system_prompt=INVESTIGATOR_PROMPT,
        model=_get_bedrock_model(),
        tools=tools or [],
    )


def investigate(
    intent_result: dict,
    incident: dict,
    mcp_tools: list = None
) -> dict:
    """Investigate an incident based on its classification.
    
    Args:
        intent_result: Result from intent classification
        incident: Original incident data
        mcp_tools: List of MCP tools from Gateway (or mock tools for testing)
        
    Returns:
        Investigation findings with root cause and evidence
    """
    intent = intent_result.get("intent", "unknown")
    confidence = intent_result.get("confidence", 0.0)
    
    # Get recommended tools for this intent
    recommended_tools = INTENT_TOOL_MAPPING.get(intent, [])
    
    # Build investigation prompt with context
    prompt = f"""Investigate the following incident:

**Incident Details**:
- Short Description: {incident.get('short_description', 'N/A')}
- Category: {incident.get('category', 'N/A')}
- Sys ID: {incident.get('sys_id', 'N/A')}

**Classification**:
- Intent: {intent}
- Confidence: {confidence}
- Reasoning: {intent_result.get('reasoning', 'N/A')}

**Recommended Tools**: {', '.join(recommended_tools) if recommended_tools else 'Use semantic search to find appropriate tools'}

**Additional Context from Incident**:
{json.dumps(incident.get('additional_info', {}), indent=2)[:1000]}

Please investigate this incident using the available tools. Start with the recommended tools and gather evidence to identify the root cause. If the incident mentions specific resource IDs (cluster IDs, job names, etc.), use those in your tool calls."""

    try:
        # If no MCP tools provided, use mock investigation
        if not mcp_tools:
            logger.warning("No MCP tools provided, using mock investigation")
            return _mock_investigation(intent, incident)
        
        # Create investigator agent with tools
        agent = create_investigator_agent(mcp_tools)
        
        # Call the agent
        result = agent(prompt)
        response_text = str(result)
        
        # Parse investigation result
        parsed, is_valid, error = parse_agent_response(response_text, "investigation")
        
        if not is_valid:
            logger.warning(f"Investigation validation failed: {error}")
            return {
                "findings": [],
                "root_cause": f"Investigation incomplete: {error}",
                "evidence_score": 0.2,
                "retry_recommended": False,
                "validation_error": error
            }
        
        logger.info(f"Investigation complete. Root cause: {parsed.get('root_cause', 'Unknown')}")
        return parsed
        
    except Exception as e:
        logger.error(f"Investigation error: {str(e)}")
        return {
            "findings": [],
            "root_cause": f"Investigation error: {str(e)}",
            "evidence_score": 0.0,
            "retry_recommended": False,
            "error": str(e)
        }


def _mock_investigation(intent: str, incident: dict) -> dict:
    """Mock investigation for testing without MCP tools."""
    mock_findings = {
        "emr_failure": {
            "findings": [
                {
                    "tool": "get_emr_logs",
                    "result": {"mock": True},
                    "summary": "EMR step failed with OutOfMemoryError"
                }
            ],
            "root_cause": "EMR step exceeded memory allocation",
            "evidence_score": 0.7,
            "retry_recommended": True,
            "recommended_action": "retry_emr with same step configuration"
        },
        "glue_etl_failure": {
            "findings": [
                {
                    "tool": "get_glue_logs",
                    "result": {"mock": True},
                    "summary": "Glue job failed with timeout"
                }
            ],
            "root_cause": "Glue job exceeded timeout threshold",
            "evidence_score": 0.75,
            "retry_recommended": True,
            "recommended_action": "retry_glue_job"
        },
        "data_missing": {
            "findings": [
                {
                    "tool": "verify_source_data",
                    "result": {"mock": True, "verified": False},
                    "summary": "Source data not found at expected path"
                }
            ],
            "root_cause": "Upstream data pipeline did not produce output",
            "evidence_score": 0.8,
            "retry_recommended": False,
            "recommended_action": "Investigate upstream pipeline"
        }
    }
    
    return mock_findings.get(intent, {
        "findings": [],
        "root_cause": "Unable to determine - mock mode",
        "evidence_score": 0.3,
        "retry_recommended": False,
        "recommended_action": ""
    })


# Alias for backward compatibility
investigate_sync = investigate
