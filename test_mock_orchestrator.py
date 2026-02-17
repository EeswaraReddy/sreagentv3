"""Mock test for hybrid orchestrator — no AWS credentials needed.

Tests the @tool functions, evaluation gates, guardrails, and intelligent routing
by calling the tool functions directly (bypassing Strands Agent + BedrockModel).
"""
import json
import sys
import os
from pathlib import Path

# Prevent BedrockModel from initializing by mocking strands
# We need to mock BEFORE importing agents
from unittest.mock import MagicMock, patch

# Mock strands module to avoid AWS connection
mock_bedrock = MagicMock()
mock_bedrock.model_id = "mock-model"

# Patch at module level before imports
with patch.dict('os.environ', {'BEDROCK_MODEL_ID': 'mock-model'}):
    # We need to mock BedrockModel constructor to return our mock
    original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__
    
    import importlib
    
    # Mock BedrockModel to avoid AWS
    class MockBedrockModel:
        def __init__(self, *args, **kwargs):
            self.model_id = kwargs.get('model_id', 'mock')
    
    class MockAgent:
        def __init__(self, *args, **kwargs):
            self.system_prompt = kwargs.get('system_prompt', '')
            self.tools = kwargs.get('tools', [])
            self.model = kwargs.get('model', None)
        def __call__(self, prompt):
            return '{"result": "mock"}'
    
    class MockTool:
        pass
    
    # Patch strands before importing our modules
    sys.modules.setdefault('strands', MagicMock())
    sys.modules.setdefault('strands.models', MagicMock())
    
    import strands
    strands.Agent = MockAgent
    strands.tool = lambda f: f  # @tool decorator = no-op
    strands.models.BedrockModel = MockBedrockModel

    # Now import our modules
    sys.path.insert(0, str(Path(__file__).parent))
    
    # Force reimport with mocks
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith('agents'):
            del sys.modules[mod_name]
    
    from agents.config import (
        SKIP_INVESTIGATION_INTENTS, FAST_TRACK_INTENTS,
        EVALUATION_THRESHOLDS, POLICY_OVERRIDES
    )
    from agents.orchestrator import (
        classify_incident, investigate_incident, evaluate_before_action,
        execute_remediation, apply_policy_decision, evaluate_before_close,
        build_rca_document, OrchestratorAgent, ORCHESTRATOR_TOOLS,
    )


def header(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def subheader(title):
    print(f"\n  --- {title} ---")


def test_tool_functions():
    """Test individual @tool functions directly."""
    header("TEST 1: Individual Tool Functions")
    passed = 0
    failed = 0
    
    # Test classify_incident
    subheader("classify_incident")
    result = classify_incident(
        incident_description="dagstatus failure Alarm for dlr_grp MWAA",
        incident_category="Data Pipeline",
    )
    assert isinstance(result, dict), "classify_incident should return dict"
    assert "intent" in result, "Missing 'intent' key"
    assert "confidence" in result, "Missing 'confidence' key"
    print(f"  ✅ classify_incident → intent={result['intent']}, confidence={result['confidence']}")
    passed += 1
    
    # Test investigate_incident (normal)
    subheader("investigate_incident (normal intent)")
    result = investigate_incident(
        incident_description="Glue ETL job failed",
        intent="glue_etl_failure",
        confidence=0.85,
    )
    assert isinstance(result, dict)
    assert "root_cause" in result
    assert not result.get("skipped", False), "Normal intent should NOT be skipped"
    print(f"  ✅ investigate_incident → root_cause={result['root_cause'][:60]}")
    passed += 1
    
    # Test investigate_incident (skip for access_denied)
    subheader("investigate_incident (access_denied → should skip)")
    result = investigate_incident(
        incident_description="Need access to prod table",
        intent="access_denied",
        confidence=0.9,
    )
    assert result.get("skipped") == True, "access_denied should be skipped!"
    print(f"  ✅ investigate_incident SKIPPED for access_denied → {result['root_cause'][:60]}")
    passed += 1
    
    # Test evaluate_before_action (approved)
    subheader("evaluate_before_action (should approve)")
    result = evaluate_before_action(
        intent="glue_etl_failure",
        confidence=0.85,
        evidence_score=0.75,
        retry_recommended=True,
    )
    assert result["approved"] == True, f"Should be approved! Got: {result}"
    print(f"  ✅ evaluate_before_action APPROVED → {result['reasoning'][:60]}")
    passed += 1
    
    # Test evaluate_before_action (rejected — low confidence)
    subheader("evaluate_before_action (low confidence → should reject)")
    result = evaluate_before_action(
        intent="unknown",
        confidence=0.3,
        evidence_score=0.5,
        retry_recommended=True,
    )
    assert result["approved"] == False, f"Should be rejected! Got: {result}"
    print(f"  ✅ evaluate_before_action REJECTED → {result['reasoning'][:60]}")
    passed += 1
    
    # Test evaluate_before_action (fast track intent)
    subheader("evaluate_before_action (access_denied → fast track reject)")
    result = evaluate_before_action(
        intent="access_denied",
        confidence=0.95,
        evidence_score=0.9,
        retry_recommended=True,
    )
    assert result["approved"] == False, "Fast track should always reject"
    print(f"  ✅ evaluate_before_action FAST_TRACK rejected → {result['reasoning'][:60]}")
    passed += 1
    
    # Test apply_policy_decision
    subheader("apply_policy_decision (normal)")
    result = apply_policy_decision(
        intent="glue_etl_failure",
        confidence=0.85,
        evidence_score=0.8,
        action_success=True,
        action_taken="retry_glue_job",
    )
    assert "decision" in result
    print(f"  ✅ apply_policy_decision → {result['decision']} (score={result['score']})")
    passed += 1
    
    # Test apply_policy_decision (override)
    subheader("apply_policy_decision (access_denied → override)")
    result = apply_policy_decision(
        intent="access_denied",
        confidence=0.9,
        evidence_score=0.0,
        action_success=False,
    )
    assert result["decision"] == "escalate", f"access_denied should always escalate! Got: {result['decision']}"
    assert result.get("override_applied") == True
    print(f"  ✅ apply_policy_decision OVERRIDE → {result['decision']}")
    passed += 1
    
    # Test evaluate_before_close (approved)
    subheader("evaluate_before_close (normal close)")
    result = evaluate_before_close(
        intent="glue_etl_failure",
        confidence=0.9,
        evidence_score=0.85,
        policy_decision="auto_close",
        policy_score=0.85,
        action_success=True,
    )
    assert result["approved_action"] == "auto_close"
    print(f"  ✅ evaluate_before_close APPROVED → {result['approved_action']}")
    passed += 1
    
    # Test evaluate_before_close (blocked — low evidence)
    subheader("evaluate_before_close (low evidence → block auto_close)")
    result = evaluate_before_close(
        intent="dag_failure",
        confidence=0.8,
        evidence_score=0.3,
        policy_decision="auto_close",
        policy_score=0.6,
        action_success=True,
    )
    assert result["approved_action"] == "human_review", f"Should be downgraded! Got: {result['approved_action']}"
    assert result.get("downgraded") == True
    print(f"  ✅ evaluate_before_close BLOCKED auto_close → {result['approved_action']}")
    passed += 1
    
    # Test evaluate_before_close (policy override)
    subheader("evaluate_before_close (access_denied → force escalate)")
    result = evaluate_before_close(
        intent="access_denied",
        confidence=0.9,
        evidence_score=0.0,
        policy_decision="escalate",
        policy_score=0.5,
        action_success=False,
    )
    assert result["approved_action"] == "escalate"
    assert result.get("override_enforced") == True
    print(f"  ✅ evaluate_before_close OVERRIDE → {result['approved_action']}")
    passed += 1
    
    # Test build_rca_document
    subheader("build_rca_document")
    result = build_rca_document(
        incident_id="INC001",
        incident_description="Glue job failed",
        intent="glue_etl_failure",
        confidence=0.85,
        root_cause="Timeout exceeded",
        evidence_score=0.75,
        action_taken="retry_glue_job",
        action_success=True,
        final_decision="auto_close",
        policy_score=0.85,
        policy_reasoning="High confidence with successful action",
    )
    assert result["decision"]["outcome"] == "auto_close"
    assert result["incident"]["sys_id"] == "INC001"
    print(f"  ✅ build_rca_document → decision={result['decision']['outcome']}")
    passed += 1
    
    print(f"\n  Results: {passed} passed, {failed} failed")
    return passed, failed


def test_guardrails():
    """Test the _apply_guardrails safety net."""
    header("TEST 2: Guardrails (_apply_guardrails)")
    passed = 0
    failed = 0
    
    # Create a minimal orchestrator (won't use Agent, just guardrails)
    orch = OrchestratorAgent.__new__(OrchestratorAgent)
    orch.mcp_tools = []
    
    # Test 1: Policy override enforcement
    subheader("Guardrail: Policy override (access_denied → escalate)")
    rca = {
        "classification": {"intent": "access_denied", "confidence": 0.9},
        "investigation": {"root_cause": "N/A", "evidence_score": 0.0},
        "decision": {"outcome": "auto_close", "score": 0.9, "reasoning": "test"},
    }
    result = orch._apply_guardrails(rca, {"sys_id": "TEST"})
    assert result["decision"]["outcome"] == "escalate", f"Should be escalate! Got: {result['decision']['outcome']}"
    assert len(result.get("guardrails", [])) > 0
    print(f"  ✅ Override enforced: auto_close → {result['decision']['outcome']}")
    passed += 1
    
    # Test 2: Block auto_close with low evidence
    subheader("Guardrail: Block auto_close with low evidence")
    rca = {
        "classification": {"intent": "dag_failure", "confidence": 0.9},
        "investigation": {"root_cause": "Timeout", "evidence_score": 0.3},
        "decision": {"outcome": "auto_close", "score": 0.7, "reasoning": "test"},
    }
    result = orch._apply_guardrails(rca, {"sys_id": "TEST"})
    assert result["decision"]["outcome"] == "human_review"
    print(f"  ✅ Auto_close blocked → {result['decision']['outcome']}")
    passed += 1
    
    # Test 3: Block auto action with low confidence
    subheader("Guardrail: Block auto action with low confidence")
    rca = {
        "classification": {"intent": "unknown", "confidence": 0.2},
        "investigation": {"root_cause": "Unknown", "evidence_score": 0.8},
        "decision": {"outcome": "auto_retry", "score": 0.5, "reasoning": "test"},
    }
    result = orch._apply_guardrails(rca, {"sys_id": "TEST"})
    assert result["decision"]["outcome"] == "human_review"
    print(f"  ✅ Auto_retry blocked → {result['decision']['outcome']}")
    passed += 1
    
    # Test 4: Allow legitimate auto_close
    subheader("Guardrail: Allow legitimate auto_close (no intervention)")
    rca = {
        "classification": {"intent": "glue_etl_failure", "confidence": 0.9},
        "investigation": {"root_cause": "Timeout exceeded", "evidence_score": 0.85},
        "decision": {"outcome": "auto_close", "score": 0.85, "reasoning": "test"},
    }
    result = orch._apply_guardrails(rca, {"sys_id": "TEST"})
    assert result["decision"]["outcome"] == "auto_close"
    assert not result.get("guardrails")
    print(f"  ✅ Legitimate auto_close passed through → {result['decision']['outcome']}")
    passed += 1
    
    print(f"\n  Results: {passed} passed, {failed} failed")
    return passed, failed


def test_config_values():
    """Test config constants are set correctly."""
    header("TEST 3: Config Validation")
    passed = 0
    
    assert "access_denied" in SKIP_INVESTIGATION_INTENTS
    print("  ✅ access_denied in SKIP_INVESTIGATION_INTENTS")
    passed += 1
    
    assert "access_denied" in FAST_TRACK_INTENTS
    print("  ✅ access_denied in FAST_TRACK_INTENTS")
    passed += 1
    
    assert "access_denied" in POLICY_OVERRIDES
    assert POLICY_OVERRIDES["access_denied"] == "escalate"
    print("  ✅ POLICY_OVERRIDES[access_denied] == escalate")
    passed += 1
    
    assert EVALUATION_THRESHOLDS["min_confidence_for_auto_action"] == 0.6
    assert EVALUATION_THRESHOLDS["min_evidence_for_auto_close"] == 0.7
    assert EVALUATION_THRESHOLDS["require_policy_approval"] == True
    print("  ✅ EVALUATION_THRESHOLDS set correctly")
    passed += 1
    
    assert len(ORCHESTRATOR_TOOLS) == 7
    tool_names = [t.__name__ for t in ORCHESTRATOR_TOOLS]
    assert "classify_incident" in tool_names
    assert "evaluate_before_action" in tool_names
    assert "evaluate_before_close" in tool_names
    print(f"  ✅ ORCHESTRATOR_TOOLS has 7 tools: {tool_names}")
    passed += 1
    
    print(f"\n  Results: {passed} passed, 0 failed")
    return passed, 0


def main():
    header("HYBRID ORCHESTRATOR — MOCK TESTS (No AWS Credentials)")
    
    total_passed = 0
    total_failed = 0
    
    p, f = test_config_values()
    total_passed += p; total_failed += f
    
    p, f = test_tool_functions()
    total_passed += p; total_failed += f
    
    p, f = test_guardrails()
    total_passed += p; total_failed += f
    
    header("FINAL SUMMARY")
    print(f"\n  Total: {total_passed + total_failed} tests")
    print(f"  ✅ Passed: {total_passed}")
    print(f"  ❌ Failed: {total_failed}")
    
    if total_failed == 0:
        print(f"\n  🎉 ALL TESTS PASSED!")
    else:
        print(f"\n  ⚠️  {total_failed} tests failed")
    
    return total_failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
