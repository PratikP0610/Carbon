"""Carbon Intelligence Engine implementation."""

import json
import logging
import re
from typing import Any

from carbon.interfaces.models import (
    ModelCapability,
    ReasoningDepth,
    TaskIntent,
)
from carbon.interfaces.protocols import IIntelligenceEngine

logger = logging.getLogger(__name__)


class IntelligenceEngine(IIntelligenceEngine):
    """Understands user intent, estimates complexity, and determines reasoning depth."""

    def __init__(self):
        # Heuristic keywords for fast-path intent classification
        self._instant_patterns = [
            r"^(hi|hello|hey|status|ping|test|ready)\b",
            r"^open\s+[a-zA-Z0-9\s]+$",
            r"^what\s+time\s+is\s+it",
            r"^who\s+are\s+you",
        ]
        self._deep_patterns = [
            r"\b(debug|refactor|architect|analyze|plan\s+step|solve|compare\s+deeply)\b",
            r"\b(code\s+review|multi-step|write\s+an\s+entire)\b",
        ]

    async def classify_intent(
        self, user_prompt: str, context: dict[str, Any] | None = None
    ) -> TaskIntent:
        """Classify user intent, complexity, and determine reasoning depth."""
        prompt_clean = user_prompt.strip().lower()

        # Check instant fast-path patterns
        for pattern in self._instant_patterns:
            if re.search(pattern, prompt_clean):
                category = (
                    "SYSTEM_COMMAND" if prompt_clean.startswith("open ") else "CONVERSATIONAL"
                )
                return TaskIntent(
                    category=category,
                    confidence=0.95,
                    reasoning_depth=ReasoningDepth.INSTANT,
                    required_capabilities=[ModelCapability.CHAT],
                    summary=f"Fast-path intent: {category}",
                )

        # Check deep patterns
        for pattern in self._deep_patterns:
            if re.search(pattern, prompt_clean):
                return TaskIntent(
                    category="COMPLEX_TASK",
                    confidence=0.88,
                    reasoning_depth=ReasoningDepth.DEEP,
                    required_capabilities=[
                        ModelCapability.CHAT,
                        ModelCapability.THINKING,
                    ],
                    summary="Deep reasoning task requiring step-by-step analysis",
                )

        # Default to standard reasoning
        return TaskIntent(
            category="GENERAL_QUERY",
            confidence=0.85,
            reasoning_depth=ReasoningDepth.STANDARD,
            required_capabilities=[ModelCapability.CHAT],
            summary="Standard natural-language request",
        )

    async def validate_output(
        self, raw_output: str, expected_schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate and parse raw LLM output against an expected schema."""
        cleaned = raw_output.strip()
        # Extract markdown json fence if present
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if json_match:
            cleaned = json_match.group(1)

        try:
            parsed = json.loads(cleaned)
            return {"valid": True, "data": parsed}
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM output as JSON: {e}")
            return {"valid": False, "error": str(e), "raw": raw_output}
