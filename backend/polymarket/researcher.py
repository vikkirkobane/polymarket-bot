"""backend/polymarket/researcher.py — AI-powered market research engine."""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from backend.database import MarketOpportunity, CopyTrader, SessionLocal
from backend.polymarket.client import PolymarketClient

logger = logging.getLogger(__name__)

_TRADER_POSITIONS_TTL = 300  # seconds — re-fetch positions at most once per 5 minutes
_MAX_CONCURRENT_POSITION_FETCHES = 8


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
        """Fetch current positions for all active copy traders, TTL-cached.

        Fetches run concurrently (bounded) — serial fetching made a single scan
        take 20+ seconds with 20 traders and amplified rate-limit risk.
        """
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

        sem = asyncio.Semaphore(_MAX_CONCURRENT_POSITION_FETCHES)

        async def _fetch(trader):
            async with sem:
                try:
                    positions = await self.client.get_trader_positions(trader.address)
                    return trader.address, {
                        "name": trader.name or trader.address[:10],
                        "positions": {p["market_id"]: p for p in positions if p.get("market_id")},
                    }
                except Exception as exc:
                    logger.warning(f"Failed to fetch positions for {trader.address}: {exc}")
                    return trader.address, None

        trader_positions = {}
        results = await asyncio.gather(*[_fetch(t) for t in traders])
        for address, data in results:
            if data is not None:
                trader_positions[address] = data

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

        # AI analysis — the analyst batches markets into concurrent chunks and
        # rotates through its own model fallback list on provider errors.
        try:
            opportunities = await self.analyst.analyze_markets(candidates, min_edge=self.min_edge)
            logger.info(f"AI analysis returned {len(opportunities)} opportunities")
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

        # Corroboration: did the price actually MOVE toward our side in 24h?
        # Momentum agreement is cheap evidence the market is heading our way —
        # it lifts confidence when aligned and flags it as a contrarian entry when not.
        await self._attach_momentum(opportunities)

        # Save ALL AI-analyzed opportunities to DB (not just high-edge ones).
        # API endpoints enforce their own display thresholds so pages always have content.
        strong = [o for o in opportunities if o.get("edge_pct", 0) >= self.min_edge]
        logger.info(f"{len(strong)} opportunities at/above {self.min_edge}% edge threshold (saving all {len(opportunities)})")
        self._save_opportunities(opportunities)  # save everything, let the UI filter
        return strong

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _attach_momentum(self, opportunities: list[dict], top_n: int = 8) -> None:
        """Fetch 24h YES-price change for the best opportunities (bounded, best-effort)."""
        ranked = sorted(opportunities, key=lambda o: o.get("edge_pct", 0), reverse=True)[:top_n]
        sem = asyncio.Semaphore(4)

        async def _one(opp: dict):
            token_id = opp.get("yes_token_id") or ""
            if not token_id:
                return
            async with sem:
                try:
                    change = await self.client.get_price_change(token_id, interval="1d")
                except Exception:
                    return
            if change is None:
                return
            opp["momentum_24h"] = change
            side = opp.get("recommended_side", "").upper()
            aligned = (side == "YES" and change > 0.01) or (side == "NO" and change < -0.01)
            opposed = (side == "YES" and change < -0.01) or (side == "NO" and change > 0.01)
            if aligned and opp.get("confidence") == "medium":
                opp["confidence"] = "high"
            elif aligned and opp.get("confidence") == "low":
                opp["confidence"] = "medium"
            elif opposed and opp.get("confidence") == "high":
                opp["confidence"] = "medium"  # fading against the tape deserves skepticism

        await asyncio.gather(*[_one(o) for o in ranked])

    def _quick_edge(self, market: dict) -> float:
        """Heuristic to prioritize markets for AI analysis.
        Returns a score > 0 if the market is worth analyzing.

        Signals used (in order of how well they predict real mis-pricing):
        1. Wide bid/ask spread — stale or thin books are where edges hide
        2. Price deviation (yes+no far from 1.0) — genuine mis-pricing signal
        3. Liquidity-weighted volume — tradable markets only
        Near-resolved extremes (<3¢ / >97¢) are dropped: the old version boosted
        them, but a 2¢ market offering "edge" is almost always settlement noise.
        """
        yes = market.get("yes_price", 0.5)
        no = market.get("no_price", 0.5)
        total = yes + no

        # Near-resolved and not accepting orders are never worth LLM tokens
        if yes < 0.03 or yes > 0.97 or no < 0.03 or no > 0.97:
            return 0.0
        if market.get("accepting_orders") is False:
            return 0.0

        # 1. Spread signal: wide spreads mean the book hasn't priced the news yet
        spread = market.get("spread") or 0.0
        spread_score = min(spread * 100, 15.0)

        # 2. Price deviation (mis-pricing signal) - prices not summing to ~1.0
        deviation = abs(1.0 - total)
        deviation_score = min(deviation * 100, 15.0)

        # 3. Liquidity floor + volume signal — can we actually get filled?
        vol_24h = market.get("volume_24h", 0)
        liquidity = market.get("liquidity", 0)
        vol_score = min(vol_24h / 5000.0, 10.0)
        liq_bonus = 3.0 if liquidity >= 10_000 else (1.0 if liquidity >= 2_000 else 0.0)

        total_score = spread_score + deviation_score + vol_score + liq_bonus
        logger.debug(
            f"Quick edge for {market.get('question', '')[:40]}...: {total_score:.2f} "
            f"(spread={spread_score:.2f}, dev={deviation_score:.2f}, vol={vol_score:.2f}, liq={liq_bonus:.1f})"
        )
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
                    if opp.get("yes_token_id"):
                        existing.yes_token_id = opp["yes_token_id"]
                    if opp.get("no_token_id"):
                        existing.no_token_id = opp["no_token_id"]
                    if opp.get("estimated_prob") is not None:
                        existing.estimated_prob = opp["estimated_prob"]
                    if opp.get("momentum_24h") is not None:
                        existing.momentum_24h = opp["momentum_24h"]
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
                        yes_token_id=opp.get("yes_token_id", ""),
                        no_token_id=opp.get("no_token_id", ""),
                        estimated_prob=opp.get("estimated_prob"),
                        momentum_24h=opp.get("momentum_24h"),
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
                    "yes_token_id": r.yes_token_id or "",
                    "no_token_id": r.no_token_id or "",
                    "estimated_prob": r.estimated_prob,
                    "momentum_24h": r.momentum_24h,
                    "scanned_at": r.scanned_at.isoformat() if r.scanned_at else None,
                    "outcome": r.outcome,
                    "outcome_marked_at": r.outcome_marked_at.isoformat() if r.outcome_marked_at else None,
                }
                for r in rows
            ]
        finally:
            db.close()
