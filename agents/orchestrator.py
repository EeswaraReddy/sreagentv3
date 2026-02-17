"""Orchestrator Agent - hybrid agent-as-tool pattern with intelligent routing.

Uses Strands Agent with specialized sub-agents exposed as @tool functions.
The orchestrator LLM decides which agents to call based on intent,
while deterministic guardrails enforce policy and evaluation gates.
"""
import json
import logging
from typing import Dict, List, Optional
from datetime import datetime

from strands import Agent, tool
from strands.models import BedrockModel

from .config import (
    MODEL_ID, INTENT_TAXONOMY, POLICY_OVERRIDES, POLICY_THRESHOLDS,
    SKIP_INVESTIGATION_INTENTS, FAST_TRACK_INTENTS, EVALUATION_THRESHOLDS,
    INTENT_TOOL_MAPPING,
)
from .prompts import ORCHESTRATOR_PROMPT
from .intent_classifier import classify_intent
from .investigator import investigate
from .action_agent import execute_action
from .policy_engine import apply_policy, build_rca

logger = logging.getLogger(__name__)

# Lazy-loaded BedrockModel — avoids import failures without AWS creds
_bedrock_model = None

def get_bedrock_model():
    """Get or create the shared BedrockModel instance."""
    global _bedrock_model
    if _bedrock_model is None:
        _bedrock_model = BedrockModel(model_id=MODEL_ID)
    return _bedrock_model

# Module-level reference to MCP tools from AgentCore Gateway.
# Set by OrchestratorAgent.__init__ before the Agent is created.
# Safe because AgentCore Runtime is single-request per container.
_mcp_tools_ref: List = []


# ============================================================
# AGENT TOOLS — each sub-agent exposed as a callable tool
# ============================================================

@tool
def classify_incident(
    incident_description: str,
    incident_category: str = "",
    incident_subcategory: str = "",
    sys_id: str = "",
) -> dict:
    """Classify an incident into an intent category using the Intent Classifier Agent.

    Always call this FIRST. Returns intent, confidence, and reasoning.

    Args:
        incident_description: The incident short description and details
        incident_category: Optional category from ServiceNow
        incident_subcategory: Optional subcategory from ServiceNow
        sys_id: Optional incident sys_id

    Returns:
        Classification with intent, confidence (0.0-1.0), and reasoning
    """
    incident = {
        "short_description": incident_description,
        "category": incident_category,
        "subcategory": incident_subcategory,
        "sys_id": sys_id,
    }
    result = classify_intent(incident)
    logger.info(f"[Tool] classify_incident → intent={result.get('intent')}, "
                f"confidence={result.get('confidence')}")
    return result


@tool
def investigate_incident(
    incident_description: str,
    intent: str,
    confidence: float,
    sys_id: str = "",
    additional_context: str = "",
) -> dict:
    """Investigate an incident to find root cause using diagnostic tools.

    DO NOT call this for access_denied or non-technical intents.
    Only call after classify_incident.

    Args:
        incident_description: The incident details
        intent: The classified intent from classify_incident
        confidence: The classification confidence score
        sys_id: Optional incident sys_id
        additional_context: Any extra context (cluster IDs, paths, etc.)

    Returns:
        Investigation with findings, root_cause, evidence_score, retry_recommended
    """
    # Check if investigation should be skipped
    if intent in SKIP_INVESTIGATION_INTENTS:
        logger.info(f"[Tool] investigate_incident SKIPPED for intent={intent}")
        return {
            "findings": [],
            "root_cause": f"Investigation skipped — intent '{intent}' handled via policy",
            "evidence_score": 0.0,
            "retry_recommended": False,
            "recommended_action": "none",
            "skipped": True,
        }

    intent_result = {"intent": intent, "confidence": confidence}
    incident = {
        "short_description": incident_description,
        "sys_id": sys_id,
        "additional_info": {"context": additional_context} if additional_context else {},
    }
    # Pass MCP tools from Gateway → investigator gets real Lambda-backed tools
    result = investigate(intent_result, incident, mcp_tools=_mcp_tools_ref)
    logger.info(f"[Tool] investigate_incident → root_cause={result.get('root_cause', 'N/A')[:80]}")
    return result


@tool
def evaluate_before_action(
    intent: str,
    confidence: float,
    evidence_score: float,
    retry_recommended: bool,
) -> dict:
    """MANDATORY evaluation gate before any remediation action.

    You MUST call this before execute_remediation.
    Checks classification confidence and evidence quality against thresholds.

    Args:
        intent: Classified intent
        confidence: Intent confidence score (0.0-1.0)
        evidence_score: Evidence quality from investigation (0.0-1.0)
        retry_recommended: Whether investigation recommends retry

    Returns:
        Evaluation with approved (bool), reasoning, and gate_name
    """
    thresholds = EVALUATION_THRESHOLDS

    # Fast-track intents never get remediation
    if intent in FAST_TRACK_INTENTS:
        return {
            "gate": "evaluate_before_action",
            "approved": False,
            "reasoning": f"Intent '{intent}' is fast-tracked — no remediation needed",
            "checks": {"fast_track": True},
        }

    # Check confidence threshold
    if confidence < thresholds["min_confidence_for_auto_action"]:
        return {
            "gate": "evaluate_before_action",
            "approved": False,
            "reasoning": f"Confidence {confidence:.2f} below threshold "
                         f"{thresholds['min_confidence_for_auto_action']}",
            "checks": {"confidence_check": False, "confidence": confidence},
        }

    # Check combined score
    combined = confidence * 0.5 + evidence_score * 0.5
    if combined < thresholds["min_combined_for_auto_action"]:
        return {
            "gate": "evaluate_before_action",
            "approved": False,
            "reasoning": f"Combined score {combined:.2f} below threshold "
                         f"{thresholds['min_combined_for_auto_action']}",
            "checks": {"combined_check": False, "combined_score": combined},
        }

    # Check if retry is recommended
    if not retry_recommended:
        return {
            "gate": "evaluate_before_action",
            "approved": False,
            "reasoning": "Investigation does not recommend retry",
            "checks": {"retry_recommended": False},
        }

    logger.info(f"[Gate] evaluate_before_action APPROVED (confidence={confidence:.2f}, "
                f"evidence={evidence_score:.2f})")
    return {
        "gate": "evaluate_before_action",
        "approved": True,
        "reasoning": f"All checks passed — confidence={confidence:.2f}, "
                     f"evidence={evidence_score:.2f}, retry recommended",
        "checks": {
            "confidence_check": True,
            "combined_check": True,
            "retry_recommended": True,
        },
    }


@tool
def execute_remediation(
    investigation_root_cause: str,
    recommended_action: str,
    incident_description: str,
    sys_id: str = "",
) -> dict:
    """Execute remediation action based on investigation findings.

    Only call if evaluate_before_action returned approved=True.

    Args:
        investigation_root_cause: Root cause from investigation
        recommended_action: The recommended action from investigation
        incident_description: Original incident description
        sys_id: Optional incident sys_id

    Returns:
        Action result with action taken, success status, and details
    """
    investigation = {
        "root_cause": investigation_root_cause,
        "recommended_action": recommended_action,
        "retry_recommended": True,
    }
    incident = {
        "short_description": incident_description,
        "sys_id": sys_id,
    }
    # Pass MCP tools from Gateway → action agent gets real Lambda-backed tools
    result = execute_action(investigation, incident, mcp_tools=_mcp_tools_ref)
    logger.info(f"[Tool] execute_remediation → action={result.get('action')}, "
                f"success={result.get('success')}")
    return result


@tool
def apply_policy_decision(
    intent: str,
    confidence: float,
    evidence_score: float,
    action_success: bool,
    action_taken: str = "none",
) -> dict:
    """Apply policy rules to determine the final decision.

    Args:
        intent: Classified intent
        confidence: Intent confidence (0.0-1.0)
        evidence_score: Evidence quality (0.0-1.0)
        action_success: Whether the action succeeded
        action_taken: Name of action taken, or 'none'

    Returns:
        Policy decision with decision, score, and reasoning
    """
    intent_result = {"intent": intent, "confidence": confidence}
    investigation = {"evidence_score": evidence_score, "findings": []}
    action_result = {
        "action": action_taken,
        "success": action_success,
        "details": {},
        "error": None,
    }
    result = apply_policy(intent_result, investigation, action_result)
    logger.info(f"[Tool] apply_policy_decision → decision={result.get('decision')}, "
                f"score={result.get('score')}")
    return result


@tool
def evaluate_before_close(
    intent: str,
    confidence: float,
    evidence_score: float,
    policy_decision: str,
    policy_score: float,
    action_success: bool,
    override_applied: bool = False,
) -> dict:
    """MANDATORY evaluation gate before updating or closing an incident.

    You MUST call this before build_rca_document.
    Enforces policy requirements and minimum thresholds for auto-close/auto-retry.

    Args:
        intent: Classified intent
        confidence: Intent confidence (0.0-1.0)
        evidence_score: Evidence quality (0.0-1.0)
        policy_decision: Decision from apply_policy_decision
        policy_score: Score from apply_policy_decision
        action_success: Whether the action succeeded
        override_applied: Whether a policy override was applied

    Returns:
        Final evaluation with approved_action (the verified decision), reasoning
    """
    thresholds = EVALUATION_THRESHOLDS

    # Policy overrides are always enforced
    if intent in POLICY_OVERRIDES:
        forced = POLICY_OVERRIDES[intent]
        logger.info(f"[Gate] evaluate_before_close: policy override → {forced}")
        return {
            "gate": "evaluate_before_close",
            "approved_action": forced,
            "original_decision": policy_decision,
            "reasoning": f"Policy override: {intent} always → {forced}",
            "override_enforced": True,
        }

    # For auto_close: enforce minimum evidence threshold
    if policy_decision == "auto_close":
        if evidence_score < thresholds["min_evidence_for_auto_close"]:
            logger.warning(f"[Gate] evaluate_before_close: BLOCKED auto_close "
                           f"(evidence={evidence_score:.2f} < {thresholds['min_evidence_for_auto_close']})")
            return {
                "gate": "evaluate_before_close",
                "approved_action": "human_review",
                "original_decision": "auto_close",
                "reasoning": f"Auto-close blocked: evidence score {evidence_score:.2f} "
                             f"below threshold {thresholds['min_evidence_for_auto_close']}",
                "override_enforced": False,
                "downgraded": True,
            }

    # For auto_retry: enforce minimum combined score
    if policy_decision == "auto_retry":
        combined = confidence * 0.5 + evidence_score * 0.5
        if combined < thresholds["min_combined_for_auto_action"]:
            logger.warning(f"[Gate] evaluate_before_close: BLOCKED auto_retry "
                           f"(combined={combined:.2f})")
            return {
                "gate": "evaluate_before_close",
                "approved_action": "human_review",
                "original_decision": "auto_retry",
                "reasoning": f"Auto-retry blocked: combined score {combined:.2f} too low",
                "override_enforced": False,
                "downgraded": True,
            }

    # Approved — pass through the policy decision
    logger.info(f"[Gate] evaluate_before_close APPROVED → {policy_decision}")
    return {
        "gate": "evaluate_before_close",
        "approved_action": policy_decision,
        "original_decision": policy_decision,
        "reasoning": f"All gates passed. Proceeding with: {policy_decision}",
        "override_enforced": False,
        "downgraded": False,
    }


@tool
def build_rca_document(
    incident_id: str,
    incident_description: str,
    intent: str,
    confidence: float,
    root_cause: str,
    evidence_score: float,
    action_taken: str,
    action_success: bool,
    final_decision: str,
    policy_score: float,
    policy_reasoning: str,
) -> dict:
    """Build the Root Cause Analysis document.

    Always call this as the LAST step.

    Args:
        incident_id: ServiceNow incident sys_id
        incident_description: Original incident description
        intent: Classified intent
        confidence: Intent confidence
        root_cause: Root cause from investigation (or 'N/A' if skipped)
        evidence_score: Evidence quality
        action_taken: Action taken (or 'none')
        action_success: Whether the action succeeded
        final_decision: The approved decision from evaluate_before_close
        policy_score: Policy score
        policy_reasoning: Policy reasoning

    Returns:
        Complete RCA document
    """
    rca = {
        "incident": {
            "sys_id": incident_id,
            "short_description": incident_description,
        },
        "classification": {
            "intent": intent,
            "confidence": confidence,
        },
        "investigation": {
            "root_cause": root_cause,
            "evidence_score": evidence_score,
        },
        "remediation": {
            "action_taken": action_taken,
            "action_success": action_success,
        },
        "decision": {
            "outcome": final_decision,
            "score": policy_score,
            "reasoning": policy_reasoning,
        },
        "timestamp": datetime.utcnow().isoformat(),
    }
    logger.info(f"[Tool] build_rca_document → decision={final_decision}")
    return rca


# All tools for the orchestrator agent
ORCHESTRATOR_TOOLS = [
    classify_incident,
    investigate_incident,
    evaluate_before_action,
    execute_remediation,
    apply_policy_decision,
    evaluate_before_close,
    build_rca_document,
]


# ============================================================
# ORCHESTRATOR AGENT CLASS
# ============================================================

class OrchestratorAgent:
    """Hybrid orchestrator: LLM-driven routing + deterministic guardrails."""

    def __init__(self, mcp_tools: Optional[List] = None):
        """Initialize the orchestrator.

        Args:
            mcp_tools: Optional list of MCP tools from AgentCore Gateway.
                       These are Lambda functions invoked via Gateway (IAM role).
        """
        # Set module-level ref so @tool functions can access Gateway tools
        global _mcp_tools_ref
        self.mcp_tools = mcp_tools or []
        _mcp_tools_ref = self.mcp_tools

        # Build tool list — include MCP tools if provided
        tools = list(ORCHESTRATOR_TOOLS)
        if self.mcp_tools:
            tools.extend(self.mcp_tools)

        # Create the Strands orchestrator agent (lazy-loaded model)
        self.agent = Agent(
            system_prompt=ORCHESTRATOR_PROMPT,
            model=get_bedrock_model(),
            tools=tools,
        )
        logger.info(f"Orchestrator initialized with {len(tools)} tools "
                     f"(agent tools + {len(self.mcp_tools)} MCP tools)")

    def orchestrate(self, incident: Dict) -> Dict:
        """Orchestrate the complete incident handling workflow.

        The LLM decides which agent tools to call based on intel routing.
        Deterministic guardrails enforce evaluation gates after.

        Args:
            incident: Incident data from ServiceNow

        Returns:
            Complete RCA with classification, investigation, action, and decision
        """
        sys_id = incident.get("sys_id", "unknown")
        start_time = datetime.utcnow()

        logger.info(f"=== Starting hybrid orchestration for incident: {sys_id} ===")

        try:
            # Build prompt for the orchestrator agent
            prompt = self._build_prompt(incident)

            # Let the LLM orchestrate using the tools
            result = self.agent(prompt)
            response_text = str(result)

            # Extract structured result from agent response
            rca = self._extract_rca(response_text, incident)

            # Apply deterministic guardrails (safety net)
            rca = self._apply_guardrails(rca, incident)

            duration = (datetime.utcnow() - start_time).total_seconds()
            logger.info(f"=== Orchestration complete for {sys_id} in {duration:.2f}s ===")

            # Add metadata
            rca["timestamp"] = start_time.isoformat()
            rca["duration_seconds"] = duration
            rca["status"] = "success"
            rca["orchestration_mode"] = "hybrid_agent_as_tool"

            return rca

        except Exception as e:
            logger.error(f"Orchestration failed for {sys_id}: {str(e)}", exc_info=True)
            return {
                "incident": {"sys_id": sys_id},
                "timestamp": start_time.isoformat(),
                "status": "error",
                "error": str(e),
                "decision": {
                    "outcome": "human_review",
                    "score": 0.0,
                    "reasoning": f"Orchestration error: {str(e)}"
                },
            }

    def _build_prompt(self, incident: Dict) -> str:
        """Build the orchestration prompt from incident data."""
        sys_id = incident.get("sys_id", "unknown")
        short_desc = incident.get("short_description", "N/A")
        description = incident.get("description", "")
        category = incident.get("category", "N/A")
        subcategory = incident.get("subcategory", "N/A")
        additional = incident.get("additional_info", {})

        return f"""Process this ServiceNow incident end-to-end:

Incident ID: {sys_id}
Short Description: {short_desc}
Description: {description[:500] if description else 'N/A'}
Category: {category}
Subcategory: {subcategory}
Additional Info: {json.dumps(additional, indent=2)[:500] if additional else 'N/A'}

Use the available tools to classify, investigate (if needed), evaluate, and resolve this incident.
Follow the intelligent routing rules in your instructions.
REMEMBER: You MUST call evaluate_before_action before any remediation and evaluate_before_close before finalizing."""

    def _extract_rca(self, response_text: str, incident: Dict) -> Dict:
        """Extract RCA from agent response."""
        # Try to find JSON in response
        try:
            if "```json" in response_text:
                start = response_text.find("```json") + 7
                end = response_text.find("```", start)
                return json.loads(response_text[start:end].strip())
            elif "{" in response_text:
                start = response_text.find("{")
                end = response_text.rfind("}") + 1
                candidate = json.loads(response_text[start:end])
                if "incident" in candidate or "decision" in candidate:
                    return candidate
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: build minimal RCA from response
        return {
            "incident": {
                "sys_id": incident.get("sys_id", "unknown"),
                "short_description": incident.get("short_description", "N/A"),
            },
            "classification": {"intent": "unknown", "confidence": 0.0},
            "investigation": {"root_cause": "See agent response", "evidence_score": 0.0},
            "remediation": {"action_taken": "none", "action_success": False},
            "decision": {
                "outcome": "human_review",
                "score": 0.0,
                "reasoning": "Could not parse structured result, defaulting to human review",
            },
            "agent_response": response_text[:2000],
        }

    def _apply_guardrails(self, rca: Dict, incident: Dict) -> Dict:
        """Apply deterministic guardrails as safety net.

        Even if the LLM made the right calls, we verify:
        1. Policy overrides are enforced
        2. Evaluation thresholds are respected
        """
        intent = rca.get("classification", {}).get("intent", "unknown")
        decision = rca.get("decision", {}).get("outcome", "human_review")
        evidence = rca.get("investigation", {}).get("evidence_score", 0.0)
        confidence = rca.get("classification", {}).get("confidence", 0.0)

        # Guardrail 1: Enforce policy overrides
        if intent in POLICY_OVERRIDES:
            forced = POLICY_OVERRIDES[intent]
            if decision != forced:
                logger.warning(f"[Guardrail] Overriding decision '{decision}' → '{forced}' "
                               f"for intent '{intent}'")
                rca["decision"]["outcome"] = forced
                rca["decision"]["reasoning"] = (
                    f"Policy override enforced: {intent} → {forced} "
                    f"(original: {decision})"
                )
                rca.setdefault("guardrails", []).append({
                    "type": "policy_override",
                    "original": decision,
                    "enforced": forced,
                })

        # Guardrail 2: Block auto_close with weak evidence
        if (rca.get("decision", {}).get("outcome") == "auto_close"
                and evidence < EVALUATION_THRESHOLDS["min_evidence_for_auto_close"]):
            logger.warning(f"[Guardrail] Blocking auto_close — evidence {evidence:.2f} too low")
            rca["decision"]["outcome"] = "human_review"
            rca["decision"]["reasoning"] = (
                f"Guardrail: auto_close blocked — evidence {evidence:.2f} "
                f"< {EVALUATION_THRESHOLDS['min_evidence_for_auto_close']}"
            )
            rca.setdefault("guardrails", []).append({
                "type": "evidence_threshold",
                "original": "auto_close",
                "enforced": "human_review",
            })

        # Guardrail 3: Block auto actions with low confidence
        if (rca.get("decision", {}).get("outcome") in ("auto_close", "auto_retry")
                and confidence < EVALUATION_THRESHOLDS["min_confidence_for_auto_action"]):
            logger.warning(f"[Guardrail] Blocking auto action — confidence {confidence:.2f} too low")
            rca["decision"]["outcome"] = "human_review"
            rca["decision"]["reasoning"] = (
                f"Guardrail: auto action blocked — confidence {confidence:.2f} "
                f"< {EVALUATION_THRESHOLDS['min_confidence_for_auto_action']}"
            )
            rca.setdefault("guardrails", []).append({
                "type": "confidence_threshold",
                "original": rca["decision"].get("outcome"),
                "enforced": "human_review",
            })

        return rca

    def _build_abort_response(self, sys_id: str, start_time: datetime,
                              reason: str, details: Dict) -> Dict:
        """Build response when orchestration is aborted."""
        return {
            "incident": {"sys_id": sys_id},
            "timestamp": start_time.isoformat(),
            "status": "aborted",
            "abort_reason": reason,
            "abort_details": details,
            "decision": {
                "outcome": "human_review",
                "score": 0.0,
                "reasoning": f"Orchestration aborted: {reason}",
            },
        }


# ============================================================
# BACKWARD-COMPATIBLE PUBLIC API
# ============================================================

def create_orchestrator(mcp_tools: Optional[List] = None) -> OrchestratorAgent:
    """Factory function to create an orchestrator agent.

    Args:
        mcp_tools: Optional list of MCP tools from Gateway

    Returns:
        Configured OrchestratorAgent instance
    """
    return OrchestratorAgent(mcp_tools=mcp_tools)


def orchestrate_incident(
    incident: Dict,
    mcp_tools: Optional[List] = None,
) -> Dict:
    """Convenience function to orchestrate incident handling.

    Args:
        incident: Incident data from ServiceNow
        mcp_tools: Optional list of MCP tools from Gateway

    Returns:
        Complete RCA with all agent results
    """
    orchestrator = create_orchestrator(mcp_tools)
    return orchestrator.orchestrate(incident)


if __name__ == "__main__":
    # Test orchestrator with sample incidents
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    test_incidents = [
        {
            "sys_id": "TEST001",
            "short_description": "dagstatus failure Alarm for dlr_grp ... MWAA",
            "category": "Data Pipeline",
            "subcategory": "Airflow",
        },
        {
            "sys_id": "TEST002",
            "short_description": "Job SPENDING_POTS... has failed Glue ETL failure",
            "category": "Data Pipeline",
            "subcategory": "ETL",
        },
        {
            "sys_id": "TEST003",
            "short_description": "I need access to production table customer_data in Athena",
            "category": "Access Request",
            "subcategory": "Database",
        },
    ]

    for incident in test_incidents:
        print(f"\n{'=' * 80}")
        print(f"Testing: {incident['short_description']}")
        print(f"{'=' * 80}")
        result = orchestrate_incident(incident)
        print(json.dumps(result, indent=2, default=str))
