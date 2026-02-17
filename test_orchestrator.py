"""Test the hybrid orchestrator agent with sample incidents."""
import json
import sys
import logging
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from agents.orchestrator import orchestrate_incident

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def test_orchestrator():
    """Test hybrid orchestrator with different issue types including intelligent routing."""
    
    test_incidents = [
        {
            "name": "MWAA DAG Failure (full pipeline)",
            "incident": {
                "sys_id": "INC001",
                "short_description": "dagstatus failure Alarm for dlr_grp ... MWAA",
                "description": "CloudWatch alarm triggered for MWAA DAG status failure in dlr_grp",
                "category": "Data Pipeline",
                "subcategory": "Airflow",
                "additional_info": {
                    "alarm_name": "dagstatus-failure-dlr_grp",
                    "service": "MWAA"
                }
            },
            "expect_investigation": True,
            "expect_decision_in": ["auto_close", "auto_retry", "escalate", "human_review"],
        },
        {
            "name": "Glue Job Failure (full pipeline)",
            "incident": {
                "sys_id": "INC002",
                "short_description": "Job SPENDING_POTS... has failed Glue ETL failure",
                "description": "Glue ETL job SPENDING_POTS has failed with error",
                "category": "Data Pipeline",
                "subcategory": "ETL",
                "additional_info": {
                    "job_name": "SPENDING_POTS",
                    "service": "Glue",
                    "error_type": "JobFailure"
                }
            },
            "expect_investigation": True,
            "expect_decision_in": ["auto_close", "auto_retry", "escalate", "human_review"],
        },
        {
            "name": "Access Request (intelligent routing — skip investigation)",
            "incident": {
                "sys_id": "INC003",
                "short_description": "I need access to production table customer_data in Athena",
                "description": "Please grant me SELECT permissions to prod_analytics.customer_data",
                "category": "Access Request",
                "subcategory": "Database",
                "additional_info": {
                    "requested_by": "john.doe@company.com",
                    "table_name": "customer_data",
                    "access_type": "SELECT"
                }
            },
            "expect_investigation": False,
            "expect_decision_in": ["escalate"],  # Policy override: access_denied → escalate
        },
        {
            "name": "Data Missing",
            "incident": {
                "sys_id": "INC004",
                "short_description": "Data is not available..., Data missing in DLR",
                "description": "Expected data not found in DLR location",
                "category": "Data Quality",
                "subcategory": "Missing Data",
                "additional_info": {
                    "location": "DLR",
                    "expected_files": ["daily_load.parquet"]
                }
            },
            "expect_investigation": True,
            "expect_decision_in": ["auto_close", "auto_retry", "escalate", "human_review"],
        },
        {
            "name": "Historical Data Missing",
            "incident": {
                "sys_id": "INC005",
                "short_description": "Data missing ... historical load",
                "description": "Historical data missing across multiple dates",
                "category": "Data Quality",
                "subcategory": "Historical Data",
                "additional_info": {
                    "missing_dates": ["2024-01-01", "2024-01-02", "2024-01-03"]
                }
            },
            "expect_investigation": True,
            "expect_decision_in": ["auto_close", "auto_retry", "escalate", "human_review"],
        },
    ]
    
    print("=" * 100)
    print("HYBRID ORCHESTRATOR AGENT TEST")
    print("Pattern: Agents-as-Tools + Evaluation Gates + Intelligent Routing")
    print("=" * 100)
    
    results = []
    
    for test_case in test_incidents:
        print(f"\n{'=' * 100}")
        print(f"TEST: {test_case['name']}")
        print(f"{'=' * 100}")
        print(f"\nIncident: {test_case['incident']['short_description']}")
        print(f"Sys ID: {test_case['incident']['sys_id']}")
        
        try:
            logger.info(f"Running orchestrator for {test_case['name']}")
            result = orchestrate_incident(test_case['incident'])
            
            print(f"\n{'=' * 50}")
            print("ORCHESTRATION RESULT")
            print(f"{'=' * 50}")
            print(json.dumps(result, indent=2, default=str))
            
            # Validate expectations
            decision = result.get("decision", {}).get("outcome", "unknown")
            has_guardrails = bool(result.get("guardrails"))
            
            print(f"\n--- Validation ---")
            print(f"  Decision: {decision}")
            print(f"  Guardrails triggered: {has_guardrails}")
            if has_guardrails:
                for g in result["guardrails"]:
                    print(f"    - {g['type']}: {g['original']} → {g['enforced']}")
            
            results.append({
                "test_name": test_case['name'],
                "sys_id": test_case['incident']['sys_id'],
                "success": "error" not in result or result.get("error") is None,
                "decision": decision,
                "result": result
            })
            
        except Exception as e:
            logger.error(f"Test failed for {test_case['name']}: {str(e)}", exc_info=True)
            results.append({
                "test_name": test_case['name'],
                "sys_id": test_case['incident']['sys_id'],
                "success": False,
                "error": str(e)
            })
    
    # Summary
    print(f"\n\n{'=' * 100}")
    print("TEST SUMMARY")
    print(f"{'=' * 100}")
    
    total = len(results)
    passed = sum(1 for r in results if r.get("success", False))
    failed = total - passed
    
    print(f"\nTotal Tests: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    
    for result in results:
        status = "✓ PASS" if result.get("success", False) else "✗ FAIL"
        decision = result.get("decision", "N/A")
        print(f"  {status} - {result['test_name']} ({result['sys_id']}) → {decision}")
        if not result.get("success", False):
            print(f"    Error: {result.get('error', 'Unknown error')}")
    
    print(f"\n{'=' * 100}\n")
    
    return results


if __name__ == "__main__":
    test_orchestrator()
