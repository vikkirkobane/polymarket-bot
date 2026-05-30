"""backend/polymarket/consensus.py — Cross-trader consensus detection."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from backend.polymarket.client import PolymarketClient
from backend.database import CopyTrader, SessionLocal

logger = logging.getLogger(__name__)

_CACHE_TTL = 300  # 5 minutes


class ConsensusEngine:
    """Identifies markets where multiple top traders share the same position."""

    def __init__(self, client: PolymarketClient):
        self.client = client
        self._cache: dict = {}                  # address → {name, positions}
        self._cache_time: Optional[datetime] = None
        self._dismissed: set[str] = set()       # "market_id:side" entries

    async def get_consensus_trades(
        self, min_traders: int = 3, leaderboard_n: int = 20
    ) -> dict:
        """Return markets where >= min_traders are on the same side, plus metadata."""
        trader_map = await self._build_trader_map(leaderboard_n)

        # Group positions by (market_id, side)
        groups: dict[tuple, dict] = {}

        for address, info in trader_map.items():
            for market_id, pos in info["positions"].items():
                side = pos.get("side", "").upper()
                if not side or not market_id:
                    continue
                if f"{market_id}:{side}" in self._dismissed:
                    continue

                key = (market_id, side)
                if key not in groups:
                    groups[key] = {
                        "market_id": market_id,
                        "question": pos.get("question", ""),
                        "side": side,
                        "traders": [],
                        "total_size": 0.0,
                    }

                groups[key]["traders"].append({
                    "name": info["name"],
                    "address": address,
                    "size": round(pos.get("size", 0), 2),
                    "avg_price": round(pos.get("avg_price", 0.5), 4),
                    "current_price": round(pos.get("current_price", 0.5), 4),
                })
                groups[key]["total_size"] += pos.get("size", 0)

        results = []
        for (market_id, side), group in groups.items():
            if len(group["traders"]) < min_traders:
                continue

            prices = [t["avg_price"] for t in group["traders"] if t["avg_price"] > 0]
            group["avg_price"] = round(sum(prices) / len(prices), 4) if prices else 0.5
            group["trader_count"] = len(group["traders"])
            group["total_size"] = round(group["total_size"], 2)
            results.append(group)

        results.sort(key=lambda x: x["trader_count"], reverse=True)

        return {
            "consensus": results,
            "total": len(results),
            "trader_pool_size": len(trader_map),
            "cache_age_seconds": int(
                (datetime.utcnow() - self._cache_time).total_seconds()
            ) if self._cache_time else None,
        }

    async def _build_trader_map(self, leaderboard_n: int) -> dict:
        """Build/refresh the address → {name, positions} map with TTL caching."""
        now = datetime.utcnow()
        if (
            self._cache_time is not None
            and (now - self._cache_time).total_seconds() < _CACHE_TTL
        ):
            logger.debug("ConsensusEngine: using cached trader positions")
            return self._cache

        # 1. Followed traders from DB
        db = SessionLocal()
        try:
            followed = db.query(CopyTrader).filter_by(active=True).all()
        finally:
            db.close()

        trader_map: dict[str, str] = {}
        for t in followed:
            trader_map[t.address] = t.name or t.address[:10] + "..."

        # 2. Leaderboard top-N (union with followed, dedup by address)
        try:
            leaderboard = await self.client.get_top_traders(limit=leaderboard_n)
            for t in leaderboard:
                if t["address"] and t["address"] not in trader_map:
                    trader_map[t["address"]] = t["name"] or t["address"][:10] + "..."
        except Exception as exc:
            logger.warning(f"ConsensusEngine: leaderboard fetch failed: {exc}")

        logger.info(f"ConsensusEngine: fetching positions for {len(trader_map)} traders")

        # 3. Fetch all positions concurrently
        async def _fetch(address: str, name: str):
            try:
                positions = await self.client.get_trader_positions(address)
                return address, name, {p["market_id"]: p for p in positions if p.get("market_id")}
            except Exception as exc:
                logger.warning(f"ConsensusEngine: position fetch failed for {address[:12]}…: {exc}")
                return address, name, {}

        raw = await asyncio.gather(*[_fetch(addr, name) for addr, name in trader_map.items()])

        cache: dict = {}
        for address, name, positions in raw:
            cache[address] = {"name": name, "positions": positions}

        self._cache = cache
        self._cache_time = now
        total_positions = sum(len(v["positions"]) for v in cache.values())
        logger.info(
            f"ConsensusEngine: cache refreshed — {len(cache)} traders, "
            f"{total_positions} total positions"
        )
        return cache

    def dismiss(self, market_id: str, side: str) -> None:
        self._dismissed.add(f"{market_id}:{side}")

    def clear_cache(self) -> None:
        """Force a fresh fetch on the next call."""
        self._cache = {}
        self._cache_time = None
