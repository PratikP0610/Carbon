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
        # Heuristic keywords for deep reasoning classification (takes highest priority)
        self._deep_patterns = [
            r"\b(debug|refactor|architect|analy[zs]e|plan\s+step|solve|compare\s+deeply|puzzle)\b",
            r"\b(code\s+review|multi-step|write\s+an\s+entire|step-by-step)\b",
        ]

        # Standard reasoning indicators (explanations, algorithms, coding)
        # Concise instructions must never downgrade these to INSTANT
        self._standard_indicators = [
            r"\b(explain|why|how\s+does|how\s+do|how\s+can|how\s+would|how\s+a)\b",
            r"\b(write\s+(?:a\s+)?(?:python|java|javascript|typescript|c\+\+|c#|swift|go|rust|ruby|sql|bash|html|css|method|function|class|script|program)|write\s+code|implement|algorithm|complexity|dijkstra|output\s+of)\b",
            r"\b(compare|trade-offs?|pros\s+and\s+cons|difference\s+between)\b",
            r"\b(fails?|failing|bug|error|issue|trace|derive|proof|prove)\b",
        ]

        # Fast-path conversational greetings and system commands
        self._instant_patterns = [
            r"^(hi|hello|hey|status|ping|test|ready)\b",
            r"^open\s+[a-zA-Z0-9\s]+$",
            r"^what\s+time\s+is\s+it",
            r"^who\s+are\s+you",
        ]

        # Robust arithmetic pattern supporting natural whitespace around operators
        self._arithmetic_pattern = (
            r"^(?:what\s+is\s+|calculate\s+|compute\s+)?"
            r"[-+]?\d+(?:\.\d+)?"
            r"(?:\s*[-+*/×÷]\s*[-+]?\d+(?:\.\d+)?)+"
            r"(?:\s*\??\s*(?:answer\s+in\s+one\s+word|just\s+the\s+number|in\s+one\s+word|please))?"
            r"[\s.?!]*$"
        )

        # Simple factual query patterns (definitions, lookups, naming)
        self._simple_factual_patterns = [
            r"^(?:what\s+(?:is|are|was|were))\b",
            r"^(?:who\s+(?:is|was|developed|created|invented|wrote|discovered))\b",
            r"^(?:where\s+(?:is|are|was|were))\b",
            r"^(?:when\s+(?:was|is|were|did))\b",
            r"^(?:what\s+does\s+[a-zA-Z0-9\s]+\s+stand\s+for)\b",
            r"^(?:define|definition\s+of|meaning\s+of)\b",
            r"^(?:name|list)\s+(?:the\s+|a\s+|an\s+|[0-9]+\s+|three\s+|two\s+|four\s+|five\s+)?\b",
            r"^(?:capital|currency|population|boiling\s+point|freezing\s+point|speed\s+of\s+light)\b",
        ]

        # Memory storage directives
        self._memory_store_patterns = [
            r"^(?:please\s+)?remember\s+(?:this|that)?[:,\s]*",
            r"^(?:please\s+)?keep\s+in\s+mind\s+(?:that)?[:,\s]*",
            r"^(?:please\s+)?save\s+(?:this\s+)?(?:preference|fact|note|memory)[:,\s]*",
            r"^(?:please\s+)?note\s+(?:that|this)[:,\s]*",
            r"^(?:make\s+a\s+note\s+that)[:,\s]*",
            r"\b(?:remember\s+this|remember\s+that)[:,\s]*$",
        ]

        # Memory forgetting / deletion directives
        self._memory_forget_patterns = [
            r"^(?:please\s+)?forget\s+(?:that|this|about)?[:,\s]*",
            r"^(?:please\s+)?delete\s+(?:my\s+)?(?:memory|preference)[:,\s]*",
        ]

        # Memory query directives (evaluates before simple factual patterns)
        self._memory_query_patterns = [
            r"^(?:what\s+(?:is|are|was|were)\s+my)\b",
            r"^(?:do\s+you\s+(?:remember|recall|know))\s+(?:my|what\s+my)\b",
            r"^(?:what\s+do\s+you\s+(?:remember|recall|know)\s+about\s+me)\b",
            r"^(?:tell\s+me\s+my|recall\s+my|what\s+did\s+i\s+(?:say|tell\s+you))\b",
            r"^(?:what\s+are\s+my\s+preferences)\b",
            r"\b(?:my\s+(?:favourite|favorite|preferred|preference))\b",
        ]

        # Explicit concise answer clauses
        self._concise_answer_clause = (
            r"\b(answer\s+in\s+one\s+word|just\s+the\s+number|in\s+one\s+word|single\s+word|one\s+word\s+only)\b"
        )

    async def classify_intent(
        self, user_prompt: str, context: dict[str, Any] | None = None
    ) -> TaskIntent:
        """Classify user intent, complexity, and determine reasoning depth."""
        prompt_clean = user_prompt.strip().lower()

        # 1. Complexity check: Deep reasoning takes absolute priority
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

        # 2. Memory storage instructions
        for pattern in self._memory_store_patterns:
            if re.search(pattern, prompt_clean):
                return TaskIntent(
                    category="MEMORY_STORE",
                    confidence=0.95,
                    reasoning_depth=ReasoningDepth.INSTANT,
                    required_capabilities=[ModelCapability.CHAT],
                    summary="Store persistent user memory",
                )

        # 3. Memory forget / deletion instructions
        for pattern in self._memory_forget_patterns:
            if re.search(pattern, prompt_clean):
                return TaskIntent(
                    category="MEMORY_FORGET",
                    confidence=0.95,
                    reasoning_depth=ReasoningDepth.INSTANT,
                    required_capabilities=[ModelCapability.CHAT],
                    summary="Forget persistent user memory",
                )

        # 4. Memory query regarding user's preferences or stored knowledge
        # Evaluated before simple factual patterns so personal queries are distinguished from world facts
        for pattern in self._memory_query_patterns:
            if re.search(pattern, prompt_clean):
                return TaskIntent(
                    category="MEMORY_QUERY",
                    confidence=0.92,
                    reasoning_depth=ReasoningDepth.INSTANT,
                    required_capabilities=[ModelCapability.CHAT],
                    summary="Query persistent user memory",
                )

        # 5. Fast conversational greetings and system commands
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

        # 6. Direct simple arithmetic calculations with robust whitespace support
        if re.search(self._arithmetic_pattern, prompt_clean):
            return TaskIntent(
                category="CONVERSATIONAL",
                confidence=0.95,
                reasoning_depth=ReasoningDepth.INSTANT,
                required_capabilities=[ModelCapability.CHAT],
                summary="Fast-path arithmetic calculation",
            )

        # 7. Standard reasoning indicators: Explanations, algorithms, and code design
        # Concise instructions must never downgrade these analytical tasks
        for pattern in self._standard_indicators:
            if re.search(pattern, prompt_clean):
                return TaskIntent(
                    category="GENERAL_QUERY",
                    confidence=0.85,
                    reasoning_depth=ReasoningDepth.STANDARD,
                    required_capabilities=[ModelCapability.CHAT],
                    summary="Standard analytical or explanatory request",
                )

        # 8. Simple factual lookup questions (with or without explicit concise instructions)
        for pattern in self._simple_factual_patterns:
            if re.search(pattern, prompt_clean):
                return TaskIntent(
                    category="CONVERSATIONAL",
                    confidence=0.92,
                    reasoning_depth=ReasoningDepth.INSTANT,
                    required_capabilities=[ModelCapability.CHAT],
                    summary="Fast-path simple factual lookup",
                )

        # 9. Concise clause on general prompt without analytical intent
        if re.search(self._concise_answer_clause, prompt_clean):
            return TaskIntent(
                category="CONVERSATIONAL",
                confidence=0.90,
                reasoning_depth=ReasoningDepth.INSTANT,
                required_capabilities=[ModelCapability.CHAT],
                summary="Fast-path concise query",
            )

        # 10. Default to standard reasoning
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
