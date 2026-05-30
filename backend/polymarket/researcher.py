"""backend/polymarket/researcher.py — AI-powered market research engine."""

import json
import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from backend.database import MarketOpportunity, CopyTrader, SessionLocal
from backend.polymarket.client import PolymarketClient

logger = logging.getLogger(__name__)

_TRADER_POSITIONS_TTL = 300  # seconds — re-fetch positions at most once per 5 minutes


class MarketResearcher:
    """Scans markets, scores edges, passes candidates to AI analyst."""

    def __init__(
        self,
        client: PolymarketClient,
        analyst,
        min_edge: float = 5.0,
        min_volume: float = 1000.0,
        limit: int = 50,
    ):
        self.client = client
        self.analyst = analyst
        self.min_edge = min_edge
        self.min_volume = min_volume
        self.limit = limit
        self._trader_positions_cache: dict = {}
        self._trader_positions_cache_time: datetime | None = None

    async def _get_copy_trader_positions(self) -> dict[str, list[dict]]:
        """Fetch current positions for all active copy traders, with a 5-minute TTL cache."""
        now = datetime.utcnow()
        if (
            self._trader_positions_cache_time is not None
            and (now - self._trader_positions_cache_time).total_seconds() < _TRADER_POSITIONS_TTL
        ):
            logger.debug("Using cached trader positions")
            return self._trader_positions_cache

        db = SessionLocal()
        try:
            traders = db.query(CopyTrader).filter_by(active=True).all()
        finally:
            db.close()

        trader_positions = {}
        for trader in traders:
            try:
                positions = await self.client.get_trader_positions(trader.address)
                trader_positions[trader.address] = {
                    "name": trader.name or trader.address[:10],
                    "positions": {p["market_id"]: p for p in positions if p.get("market_id")},
                }
            except Exception as exc:
                logger.warning(f"Failed to fetch positions for {trader.address}: {exc}")

        self._trader_positions_cache = trader_positions
        self._trader_positions_cache_time = now
        logger.info(f"Refreshed trader positions cache for {len(trader_positions)} traders")
        return trader_positions

    def _check_trader_alignment(self, opportunity: dict, trader_positions: dict) -> list[dict]:
        """Check if any top traders are aligned with this opportunity.
        Returns list of matching traders with their position info."""
        matches = []
        market_id = opportunity.get("market_id")
        recommended_side = opportunity.get("recommended_side", "").upper()
        
        if not market_id or not recommended_side:
            return matches
        
        for address, data in trader_positions.items():
            if market_id in data["positions"]:
                trader_pos = data["positions"][market_id]
                trader_side = trader_pos.get("side", "").upper()
                # Match if trader is on same side as recommendation
                if trader_side == recommended_side:
                    matches.append({
                        "trader_name": data["name"],
                        "trader_address": address,
                        "side": trader_side,
                        "size": trader_pos.get("size", 0),
                    })
        return matches

    async def run_scan(self) -> list[dict]:
        """Full scan: fetch → filter → AI analyze → match traders → save → return."""
        logger.info("Starting market research scan...")
        markets = await self.client.get_markets(limit=self.limit, min_volume=self.min_volume)
        logger.info(f"Fetched {len(markets)} markets above volume threshold")

        if not markets:
            return []

        # Pre-filter: only include markets with some price inefficiency signal
        candidates = [m for m in markets if self._quick_edge(m) > 0]
        logger.info(f"{len(candidates)} markets passed quick-edge filter")

        if not candidates:
            return []
        
        # Fetch copy trader positions for alignment checking
        logger.info("Fetching copy trader positions for alignment check...")
        trader_positions = await self._get_copy_trader_positions()
        logger.info(f"Fetched positions for {len(trader_positions)} active traders")

        # AI analysis (batch) — retry with a backup OpenRouter model if primary returns nothing
        try:
            opportunities = await self.analyst.analyze_markets(candidates, min_edge=self.min_edge)
            logger.info(f"AI analysis returned {len(opportunities)} opportunities")

            # Fallback: if primary model returned nothing, retry with a different OpenRouter model
            if not opportunities and self.analyst.model_name == "openrouter":
                logger.warning("Primary OpenRouter model returned 0 results — retrying with qwen/qwen3-next-80b-a3b-instruct:free")
                from backend.analyst.factory import OpenRouterAnalyst
                from backend.config import get_settings as _gs
                _s = _gs()
                fallback = OpenRouterAnalyst(
                    api_key=_s.openrouter_api_key,
                    model="qwen/qwen3-next-80b-a3b-instruct:free",
                )
                opportunities = await fallback.analyze_markets(candidates, min_edge=self.min_edge)
                logger.info(f"OpenRouter fallback returned {len(opportunities)} opportunities")
            for opp in opportunities:
                logger.info(
                    f"  → {opp.get('question','?')[:50]} | edge={opp.get('edge_pct',0):.1f}% "
                    f"conf={opp.get('confidence','?')} side={opp.get('recommended_side','?')}"
                )
        except Exception as exc:
            logger.error(f"AI analysis failed: {exc}")
            opportunities = []

        # Check trader alignment for each opportunity
        for opp in opportunities:
            matches = self._check_trader_alignment(opp, trader_positions)
            if matches:
                opp["trader_matches"] = matches
                opp["trader_match_count"] = len(matches)
                logger.info(f"Opportunity '{opp.get('question', '')[:40]}' aligned with {len(matches)} top trader(s)")

        # Save ALL AI-analyzed opportunities to DB (not just high-edge ones).
        # API endpoints enforce their own display thresholds so pages always have content.
        strong = [o for o in opportunities if o.get("edge_pct", 0) >= self.min_edge]
        logger.info(f"{len(strong)} opportunities at/above {self.min_edge}% edge threshold (saving all {len(opportunities)})")
        self._save_opportunities(opportunities)  # save everything, let the UI filter
        return strong

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _quick_edge(self, market: dict) -> float:
        """Heuristic to prioritize markets for AI analysis.
        Returns a score > 0 if the market is worth analyzing.
        """
        yes = market.get("yes_price", 0.5)
        no = market.get("no_price", 0.5)
        total = yes + no
        
        # 1. Price deviation (mis-pricing signal) - prices not summing to ~1.0
        deviation = abs(1.0 - total)
        edge_score = deviation * 100
        
        # 2. Volume signal (high volume means more social relevance)
        vol_24h = market.get("volume_24h", 0)
        vol_score = min(vol_24h / 5000.0, 10.0)  # Cap at 10.0
        
        # 3. Extreme pricing signal (opportunities near 0% or 100% can still be valuable)
        extreme_score = 0.0
        if yes < 0.1 or yes > 0.9 or no < 0.1 or no > 0.9:
            extreme_score = 2.0  # Boost extreme prices
        
        total_score = edge_score + vol_score + extreme_score
        logger.debug(f"Quick edge for {market.get('question', '')[:40]}...: {total_score:.2f} (dev={edge_score:.2f}, vol={vol_score:.2f}, extreme={extreme_score:.2f})")
        return total_score

    def _save_opportunities(self, opportunities: list[dict]):
        """Upsert opportunities — update the existing record for a market if already scanned today.
        Also purges records older than 7 days so the list stays fresh."""
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            cutoff = now - timedelta(days=7)

            # Purge anything older than 2 days before inserting fresh data
            deleted = (
                db.query(MarketOpportunity)
                .filter(MarketOpportunity.scanned_at < cutoff)
                .filter(MarketOpportunity.outcome.is_(None))  # never purge reviewed trades
                .delete(synchronize_session=False)
            )
            if deleted:
                logger.info(f"Purged {deleted} stale opportunities (older than 7 days)")
            db.commit()

            today_start = datetime(now.year, now.month, now.day)

            for opp in opportunities:
                market_id = opp.get("market_id", "")
                trader_matches = json.dumps(opp["trader_matches"]) if opp.get("trader_matches") else None

                existing = (
                    db.query(MarketOpportunity)
                    .filter(
                        MarketOpportunity.market_id == market_id,
                        MarketOpportunity.scanned_at >= today_start,
                    )
                    .first()
                )

                if existing:
                    existing.edge_pct = opp.get("edge_pct", 0)
                    existing.confidence = opp.get("confidence", "low")
                    existing.analysis = opp.get("reasoning", "")
                    existing.trader_matches = trader_matches
                    existing.scanned_at = now
                    existing.yes_price = opp.get("yes_price", existing.yes_price)
                    existing.no_price = opp.get("no_price", existing.no_price)
                    if opp.get("end_date"):
                        existing.end_date = opp["end_date"]
                else:
                    db.add(MarketOpportunity(
                        market_id=market_id,
                        question=opp.get("question", ""),
                        yes_price=opp.get("yes_price", 0.5),
                        no_price=opp.get("no_price", 0.5),
                        volume_24h=opp.get("volume_24h", 0),
                        edge_pct=opp.get("edge_pct", 0),
                        side=opp.get("recommended_side", ""),
                        confidence=opp.get("confidence", "low"),
                        model_used=opp.get("model_used", ""),
                        analysis=opp.get("reasoning", ""),
                        trader_matches=trader_matches,
                        end_date=opp.get("end_date", ""),
                        scanned_at=now,
                    ))
            db.commit()
        finally:
            db.close()

    def get_latest_opportunities(self, limit: int = 30) -> list[dict]:
        db = SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(days=7)
            rows = (
                db.query(MarketOpportunity)
                .filter(MarketOpportunity.scanned_at >= cutoff)
                .order_by(MarketOpportunity.scanned_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "market_id": r.market_id,
                    "question": r.question,
                    "yes_price": r.yes_price,
                    "no_price": r.no_price,
                    "volume_24h": r.volume_24h,
                    "edge_pct": r.edge_pct,
                    "side": r.side,
                    "confidence": r.confidence,
                    "model_used": r.model_used,
                    "analysis": r.analysis,
                    "trader_matches": json.loads(r.trader_matches) if r.trader_matches else [],
                    "end_date": r.end_date,
                    "scanned_at": r.scanned_at.isoformat() if r.scanned_at else None,
                    "outcome": r.outcome,
                    "outcome_marked_at": r.outcome_marked_at.isoformat() if r.outcome_marked_at else None,
                }
                for r in rows
            ]
        finally:
            db.close()
