"""Intent Classifier Agent - classifies incidents using the intent taxonomy."""
import json
import logging

from strands import Agent
from strands.models import BedrockModel

from .config import MODEL_ID, INTENT_TAXONOMY
from .schemas import parse_agent_response
from .prompts import INTENT_CLASSIFIER_PROMPT

logger = logging.getLogger(__name__)

# Lazy-loaded BedrockModel — avoids import failures without AWS creds
_bedrock_model = None

def _get_bedrock_model():
    global _bedrock_model
    if _bedrock_model is None:
        _bedrock_model = BedrockModel(model_id=MODEL_ID)
    return _bedrock_model

# Lazy-created agent — avoids immediate Bedrock connection
_intent_classifier_agent = None

def _get_classifier_agent():
    global _intent_classifier_agent
    if _intent_classifier_agent is None:
        _intent_classifier_agent = Agent(
            system_prompt=INTENT_CLASSIFIER_PROMPT,
            model=_get_bedrock_model(),
        )
    return _intent_classifier_agent


def classify_intent(incident: dict) -> dict:
    """Classify an incident into an intent category.
    
    Args:
        incident: Incident data containing short_description and other fields
        
    Returns:
        Classification result with intent, confidence, and reasoning
    """
    # Build classification prompt
    short_description = incident.get("short_description", "")
    description = incident.get("description", "")
    category = incident.get("category", "")
    subcategory = incident.get("subcategory", "")
    
    prompt = f"""Classify the following incident:

**Short Description**: {short_description}

**Description**: {description[:500] if description else 'N/A'}

**Category**: {category or 'N/A'}
**Subcategory**: {subcategory or 'N/A'}

Analyze this incident and provide your classification in JSON format."""

    try:
        # Call the lazy-loaded agent
        result = _get_classifier_agent()(prompt)
        response_text = str(result)
        
        # Parse and validate response
        parsed, is_valid, error = parse_agent_response(response_text, "intent")
        
        if not is_valid:
            logger.warning(f"Intent classification validation failed: {error}")
            return {
                "intent": "unknown",
                "confidence": 0.1,
                "reasoning": f"Classification failed validation: {error}",
                "validation_error": error
            }
        
        # Ensure intent is in taxonomy
        if parsed.get("intent") not in INTENT_TAXONOMY:
            logger.warning(f"Unknown intent: {parsed.get('intent')}, defaulting to 'unknown'")
            parsed["intent"] = "unknown"
            parsed["confidence"] = min(parsed.get("confidence", 0.5), 0.5)
        
        logger.info(f"Classified incident as '{parsed['intent']}' with confidence {parsed['confidence']}")
        return parsed
        
    except Exception as e:
        logger.error(f"Intent classification error: {str(e)}")
        return {
            "intent": "unknown",
            "confidence": 0.0,
            "reasoning": f"Classification error: {str(e)}",
            "error": str(e)
        }


# Alias for backward compatibility
classify_intent_sync = classify_intent
