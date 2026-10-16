"""backend/analyst/base.py — Abstract base for all AI analysts."""

import asyncio
import json
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT = """You are an expert prediction market analyst. Estimate the TRUE probability of each Polymarket market resolving YES, then compare against what the market currently charges.

Markets to analyze (prices are in $0.00–$1.00 and equal implied probability):
{markets_json}

For EACH market, decide:
1. estimated_prob — your best estimate of the true probability of YES (0.0–1.0), using the question, description, and end_date. If the event is after your knowledge or you have no real signal, set estimated_prob to the current yes_price (that means "no edge").
2. confidence — "low", "medium", or "high". Only claim high confidence with concrete, verifiable knowledge.
3. reasoning — one sentence (max 30 words).

Rules:
- Return exactly one object for EVERY market listed above — never omit markets.
  Edge filtering is done against live prices afterwards, so an estimate that
  matches the current price is fine (it just means "no edge").
- Confidence: only claim high confidence with concrete, verifiable knowledge.
- Prefer markets ending within 60 days; far-future or longshot (<3% or >97%) markets rarely offer real edge.
- Do not invent news. Never estimate 0.0 or 1.0 exactly.

Respond with ONLY a valid JSON array (one object per market), no markdown, no commentary. Example:
[
  {{
    "market_id": "0xabc...",
    "estimated_prob": 0.72,
    "confidence": "medium",
    "reasoning": "Incumbent won all 6 prior primaries in this state."
  }}
]
"""


class BaseAnalyst(ABC):
    """Abstract analyst — subclasses implement _call_model().

    Markets are analyzed in small concurrent chunks so one oversized request
    can't blow the output-token budget of free-tier models (the historical
    cause of empty scans), and edge is computed from the model's probability
    estimate instead of trusting its arithmetic.
    """

    model_name: str = "base"
    chunk_size: int = 10          # markets per LLM call
    max_concurrency: int = 3      # parallel LLM calls per scan
    max_opportunities_per_chunk: int = 4

    async def analyze_markets(self, markets: list[dict], min_edge: float = 3.0) -> list[dict]:
        """Analyze markets in concurrent chunks and return opportunity dicts."""
        if not markets:
            return []

        # Skip markets not currently accepting orders; keep everything else —
        # chunking lets us afford a wider funnel than the old single-prompt cap of 30.
        candidates = [m for m in markets if m.get("accepting_orders", True)][:60]
        if not candidates:
            return []

        chunks = [candidates[i:i + self.chunk_size] for i in range(0, len(candidates), self.chunk_size)]
        sem = asyncio.Semaphore(self.max_concurrency)

        async def _run(chunk: list[dict]) -> list[dict]:
            async with sem:
                try:
                    return await self._analyze_chunk(chunk, min_edge)
                except Exception as exc:
                    logger.warning(f"[{self.model_name}] chunk failed: {str(exc)[:160]}")
                    return []

        results = await asyncio.gather(*[_run(c) for c in chunks])
        opportunities = [o for chunk in results for o in chunk]
        logger.info(f"[{self.model_name}] {len(opportunities)} opportunities across {len(chunks)} chunks")
        return opportunities

    async def _analyze_chunk(self, markets: list[dict], min_edge: float) -> list[dict]:
        prompt = ANALYSIS_PROMPT.format(
            markets_json=json.dumps([self._slim_market(m) for m in markets], indent=2),
            min_edge_pct=max(1.0, min_edge / 2),  # surface borderline calls; we filter below
            max_opps=self.max_opportunities_per_chunk,
        )
        response_text = await self._call_model(prompt)
        logger.debug(f"[{self.model_name}] Raw response length: {len(response_text)}")
        parsed = self._parse_json_array(response_text)
        return self._build_opportunities(parsed, markets)

    @staticmethod
    def _slim_market(m: dict) -> dict:
        """Compact per-market context — description trimmed to control prompt size."""
        slim = {
            "market_id": m["market_id"],
            "question": m["question"],
            "yes_price": m["yes_price"],
        }
        if m.get("description"):
            slim["description"] = m["description"][:220]
        if m.get("end_date"):
            slim["end_date"] = str(m["end_date"])[:10]
        if m.get("best_ask"):
            slim["yes_ask_price"] = m["best_ask"]
        if m.get("best_bid"):
            slim["yes_bid_price"] = m["best_bid"]
        slim["volume_24h"] = int(m.get("volume_24h", 0))
        return slim

    @abstractmethod
    async def _call_model(self, prompt: str) -> str:
        """Send prompt to AI model and return raw text response."""
        ...

    # ── Response handling ──────────────────────────────────────────────────────

    def _build_opportunities(self, parsed: list, original_markets: list[dict]) -> list[dict]:
        """Turn raw model rows into enriched opportunity dicts.

        Accepts both the new probability format (estimated_prob) and the legacy
        format (recommended_side + edge_pct). Edge is re-derived from live
        market prices so stale/hallucinated numbers never reach the DB.
        """
        market_lookup = {m["market_id"]: m for m in original_markets}
        results = []
        for opp in parsed:
            if not isinstance(opp, dict):
                continue
            mid = opp.get("market_id", "")
            orig = market_lookup.get(mid)
            if not orig:
                continue  # model invented an id — drop it

            yes_price = orig.get("yes_price", 0.5)
            no_price = orig.get("no_price", 1 - yes_price)
            # Executable prices when available: buy YES at ask, buy NO at (1 - bid)
            yes_cost = orig.get("best_ask") or yes_price
            no_cost = (1.0 - orig.get("best_bid")) if orig.get("best_bid") else no_price

            est = opp.get("estimated_prob")
            if est is not None:
                try:
                    est = min(max(float(est), 0.0), 1.0)
                except (TypeError, ValueError):
                    continue
                edge_yes = (est - yes_cost) * 100
                edge_no = ((1 - est) - no_cost) * 100
                if edge_yes >= edge_no:
                    side, edge = "YES", edge_yes
                else:
                    side, edge = "NO", edge_no
                if edge < 1.0:  # below noise floor — no trade
                    continue
                opp["recommended_side"] = side
                opp["edge_pct"] = round(edge, 1)
            else:
                # Legacy format: trust side/edge but sanitize against live prices
                side = str(opp.get("recommended_side", "")).upper()
                if side not in ("YES", "NO"):
                    continue
                opp["recommended_side"] = side
                opp["edge_pct"] = round(float(opp.get("edge_pct", 0) or 0), 1)

            enriched = {
                "market_id": mid,
                "question": orig.get("question", opp.get("question", "")),
                "recommended_side": opp["recommended_side"],
                "edge_pct": opp["edge_pct"],
                "yes_price": yes_price,
                "no_price": no_price,
                "yes_token_id": orig.get("yes_token_id", ""),
                "no_token_id": orig.get("no_token_id", ""),
                "volume_24h": orig.get("volume_24h", 0),
                "liquidity": orig.get("liquidity", 0),
                "spread": orig.get("spread"),
                "event_title": orig.get("event_title", ""),
                "neg_risk": orig.get("neg_risk", False),
                "end_date": orig.get("end_date", ""),
                "confidence": str(opp.get("confidence", "low")).lower(),
                "reasoning": opp.get("reasoning", ""),
                "estimated_prob": est,
                "model_used": self.model_name,
            }
            results.append(enriched)
        return results

    def _parse_json_array(self, text: str) -> list:
        """Parse a JSON array from model output with fence-stripping and repair."""
        text = text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"[{self.model_name}] JSON parse error: {e}. Attempting repair...")
            repaired = self._repair_json(text)
            if repaired:
                try:
                    parsed = json.loads(repaired)
                    logger.info(f"[{self.model_name}] Successfully repaired truncated JSON")
                except json.JSONDecodeError:
                    import re
                    match = re.search(r"\[.*\]", text, re.DOTALL)
                    if match:
                        try:
                            parsed = json.loads(match.group())
                        except Exception:
                            logger.warning(f"[{self.model_name}] All JSON parsing attempts failed")
                            return []
                    else:
                        logger.warning(f"[{self.model_name}] No JSON array found in response")
                        return []
            else:
                logger.warning(f"[{self.model_name}] Could not repair JSON. Preview: {text[:200]}...")
                return []

        if isinstance(parsed, dict):  # models sometimes wrap in {"opportunities": [...]}
            for v in parsed.values():
                if isinstance(v, list):
                    parsed = v
                    break
        if not isinstance(parsed, list):
            return []
        return parsed

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
