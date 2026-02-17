"""Sample incident simulation — verifies full end-to-end flow (mock mode)."""
import sys
import json
from unittest.mock import MagicMock

# Mock strands to avoid AWS creds
class MockBedrockModel:
    def __init__(self, *args, **kwargs):
        self.model_id = kwargs.get("model_id", "mock")

class MockAgent:
    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools", [])
        self.model = kwargs.get("model", None)
    def __call__(self, prompt):
        return '{"result": "mock"}'

sys.modules.setdefault("strands", MagicMock())
sys.modules.setdefault("strands.models", MagicMock())
import strands
strands.Agent = MockAgent
strands.tool = lambda f: f
strands.models.BedrockModel = MockBedrockModel

for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("agents"):
        del sys.modules[mod_name]

from agents.orchestrator import (
    classify_incident, investigate_incident, evaluate_before_action,
    execute_remediation, apply_policy_decision, evaluate_before_close,
    build_rca_document, OrchestratorAgent
)
from agents.gateway_client import GatewayToolProvider


def main():
    print("=" * 60)
    print("  SAMPLE INCIDENT: Glue ETL Job Failure")
    print("=" * 60)

    incident = {
        "sys_id": "INC0012345",
        "short_description": "Glue job data_reconciliation failed with timeout after 2hrs",
        "description": "The Glue ETL job data_reconciliation failed at 2:15 AM.",
        "category": "Data Pipeline",
        "subcategory": "ETL",
    }

    # Step 0: Gateway connection (mock - no endpoint set)
    print("\n>> Step 0: GatewayToolProvider")
    gw = GatewayToolProvider()
    tools = gw.start()
    print(f"   Connected: {gw.is_connected}")
    print(f"   Tools: {len(tools)} (mock mode - no GATEWAY_ENDPOINT)")

    # Step 1: Classification
    print("\n>> Step 1: classify_incident")
    cls = classify_incident(
        incident_description=incident["short_description"],
        incident_category=incident["category"],
    )
    print(f"   Intent: {cls['intent']}    Confidence: {cls['confidence']}")

    # Step 2: Investigation (mock mode - no Gateway)
    print("\n>> Step 2: investigate_incident")
    inv = investigate_incident(
        incident_description=incident["short_description"],
        intent="glue_etl_failure",
        confidence=0.88,
    )
    print(f"   Root Cause: {inv['root_cause']}")
    print(f"   Evidence Score: {inv['evidence_score']}")
    print(f"   Retry Recommended: {inv['retry_recommended']}")

    # Step 3: Evaluation gate
    print("\n>> Step 3: evaluate_before_action")
    gate = evaluate_before_action(
        intent="glue_etl_failure",
        confidence=0.88,
        evidence_score=inv["evidence_score"],
        retry_recommended=inv["retry_recommended"],
    )
    print(f"   Approved: {gate['approved']}")
    print(f"   Reasoning: {gate['reasoning']}")

    # Step 4: Execute remediation (mock)
    print("\n>> Step 4: execute_remediation")
    action = execute_remediation(
        investigation_root_cause=inv["root_cause"],
        recommended_action="retry_glue_job",
        incident_description=incident["short_description"],
        sys_id=incident["sys_id"],
    )
    print(f"   Action: {action['action']}")
    print(f"   Success: {action['success']}")

    # Step 5: Policy decision
    print("\n>> Step 5: apply_policy_decision")
    policy = apply_policy_decision(
        intent="glue_etl_failure",
        confidence=0.88,
        evidence_score=inv["evidence_score"],
        action_success=action["success"],
        action_taken=action["action"],
    )
    print(f"   Decision: {policy['decision']}")
    print(f"   Score: {policy['score']}")

    # Step 6: Final gate
    print("\n>> Step 6: evaluate_before_close")
    final_gate = evaluate_before_close(
        intent="glue_etl_failure",
        confidence=0.88,
        evidence_score=inv["evidence_score"],
        policy_decision=policy["decision"],
        policy_score=policy["score"],
        action_success=action["success"],
    )
    print(f"   Final Approved Action: {final_gate['approved_action']}")

    # Step 7: Build RCA
    print("\n>> Step 7: build_rca_document")
    rca = build_rca_document(
        incident_id=incident["sys_id"],
        incident_description=incident["short_description"],
        intent="glue_etl_failure",
        confidence=0.88,
        root_cause=inv["root_cause"],
        evidence_score=inv["evidence_score"],
        action_taken=action["action"],
        action_success=action["success"],
        final_decision=final_gate["approved_action"],
        policy_score=policy["score"],
        policy_reasoning=policy.get("reasoning", ""),
    )
    print(f"   Incident: {rca['incident']['sys_id']}")
    print(f"   Outcome: {rca['decision']['outcome']}")
    print(f"   Guardrails Applied: {len(rca.get('guardrails', []))}")

    # Cleanup
    gw.stop()

    print("\n" + "=" * 60)
    print("  SAMPLE INCIDENT COMPLETE")
    print(f"  Flow: classify -> investigate -> gate -> remediate -> policy -> gate -> RCA")
    print(f"  Result: {final_gate['approved_action'].upper()}")
    print("=" * 60)

    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
