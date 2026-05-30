"""backend/analyst/base.py — Abstract base for all AI analysts."""

from abc import ABC, abstractmethod
import json
import logging

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT = """You are an expert prediction market analyst. Analyze the following Polymarket markets and identify the best trading opportunities.

For each market, evaluate:
1. Whether the current YES/NO prices reflect the true probability
2. Any obvious mis-pricing based on your knowledge
3. The recommended trade (YES, NO, or SKIP)
4. Edge percentage (how much better than market price you estimate the true probability to be)
5. Confidence level (low/medium/high)

Markets to analyze:
{markets_json}

Respond with ONLY valid JSON array, no markdown, no explanation outside the JSON. Example:
[
  {{
    "market_id": "0xabc...",
    "question": "Will X happen?",
    "yes_price": 0.65,
    "no_price": 0.35,
    "recommended_side": "NO",
    "edge_pct": 12.5,
    "confidence": "high",
    "reasoning": "Brief explanation of the edge"
  }}
]

Only include markets where you see a genuine edge (edge_pct > {min_edge_pct}%). Return empty array [] if none qualify.
"""


class BaseAnalyst(ABC):
    """Abstract analyst — subclasses implement _call_model()."""

    model_name: str = "base"

    async def analyze_markets(self, markets: list[dict], min_edge: float = 3.0) -> list[dict]:
        """Analyze a list of markets and return opportunity dicts."""
        if not markets:
            return []

        # Build prompt with only essential fields to save tokens
        slim = [
            {
                "market_id": m["market_id"],
                "question": m["question"],
                "yes_price": m["yes_price"],
                "no_price": m["no_price"],
                "volume_24h": m["volume_24h"],
            }
            for m in markets[:30]  # Cap at 30 to stay within context limits
        ]
        # Use 1% prompt threshold — we want the AI to surface anything useful;
        # the API endpoints enforce their own display thresholds (3%+ for Best Picks, etc.)
        prompt = ANALYSIS_PROMPT.format(markets_json=json.dumps(slim, indent=2), min_edge_pct=1.0)

        try:
            response_text = await self._call_model(prompt)
            logger.debug(f"[{self.model_name}] Raw response length: {len(response_text)}")
            opportunities = self._parse_response(response_text, markets)
            logger.info(f"[{self.model_name}] Parsed {len(opportunities)} opportunities from response")
            return opportunities
        except Exception as exc:
            logger.error(f"[{self.model_name}] Analysis failed: {exc}")
            return []

    @abstractmethod
    async def _call_model(self, prompt: str) -> str:
        """Send prompt to AI model and return raw text response."""
        ...

    def _parse_response(self, text: str, original_markets: list[dict]) -> list[dict]:
        """Parse JSON from model response, enrich with original market data."""
        text = text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"[{self.model_name}] JSON parse error: {e}. Attempting repair...")
            # Try to repair truncated JSON
            repaired = self._repair_json(text)
            if repaired:
                try:
                    parsed = json.loads(repaired)
                    logger.info(f"[{self.model_name}] Successfully repaired truncated JSON")
                except json.JSONDecodeError:
                    # Try regex extraction as fallback
                    import re
                    match = re.search(r"\[.*\]", text, re.DOTALL)
                    if match:
                        try:
                            parsed = json.loads(match.group())
                        except Exception as e2:
                            logger.warning(f"[{self.model_name}] All JSON parsing attempts failed")
                            return []
                    else:
                        logger.warning(f"[{self.model_name}] No JSON array found in response")
                        return []
            else:
                logger.warning(f"[{self.model_name}] Could not repair JSON. Preview: {text[:200]}...")
                return []

        # Build lookup for enrichment
        market_lookup = {m["market_id"]: m for m in original_markets}
        results = []
        for opp in parsed:
            if not isinstance(opp, dict):
                continue
            mid = opp.get("market_id", "")
            orig = market_lookup.get(mid, {})
            opp["volume_24h"] = orig.get("volume_24h", opp.get("volume_24h", 0))
            opp["end_date"] = orig.get("end_date", opp.get("end_date", ""))
            opp["yes_price"] = orig.get("yes_price", opp.get("yes_price", 0.5))
            opp["no_price"] = orig.get("no_price", opp.get("no_price", 0.5))
            opp["model_used"] = self.model_name
            results.append(opp)
        return results

    def _repair_json(self, text: str) -> str | None:
        """Attempt to repair truncated JSON by closing open structures."""
        text = text.strip()

        # Count open brackets/braces
        open_brackets = text.count('[') - text.count(']')
        open_braces = text.count('{') - text.count('}')
        open_strings = text.count('"') % 2  # Odd number means unclosed string

        # Close unclosed string
        if open_strings:
            text += '"'

        # Close any unclosed objects
        for _ in range(open_braces):
            if text.rfind(',') > text.rfind('}'):
                text = text.rstrip(',')
            text += '}'

        # Close unclosed array
        for _ in range(open_brackets):
            text = text.rstrip(',')
            text += ']'

        # Try to validate
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            return None
