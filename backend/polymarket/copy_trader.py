"""backend/polymarket/copy_trader.py — Copy trading engine."""

import asyncio
import logging
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session

from backend.polymarket.client import PolymarketClient
from backend.database import CopyTrader, CopySignal, TraderPositionState, SessionLocal

logger = logging.getLogger(__name__)


class CopyTradingEngine:
    """Monitors top traders and generates copy signals."""

    def __init__(self, client: PolymarketClient, scale_factor: float = 0.1, max_bet: float = 100.0):
        self.client = client
        self.scale_factor = scale_factor
        self.max_bet = max_bet

    async def add_trader_with_stats(self, address: str, name: Optional[str] = None) -> CopyTrader | None:
        """Add a trader and fetch their stats from API."""
        # First fetch stats from API
        stats = await self.client.get_trader_stats(address)
        
        db = SessionLocal()
        try:
            existing = db.query(CopyTrader).filter_by(address=address).first()
            if existing:
                existing.active = True
                if stats:
                    existing.roi = stats.get("roi", 0)
                    existing.total_profit = stats.get("profit", 0)
                    existing.volume = stats.get("volume", 0)
                    existing.win_rate = stats.get("win_rate", 0)
                    if stats.get("name"):
                        existing.name = stats["name"]
                db.commit()
                return existing
            
            # Create new trader with stats
            ct = CopyTrader(
                address=address,
                name=name or (stats.get("name") if stats else None) or address[:10] + "...",
                roi=stats.get("roi", 0) if stats else 0,
                total_profit=stats.get("profit", 0) if stats else 0,
                volume=stats.get("volume", 0) if stats else 0,
                win_rate=stats.get("win_rate", 0) if stats else 0,
                active=True,
            )
            db.add(ct)
            db.commit()
            db.refresh(ct)
            logger.info(f"Added trader {ct.name} with ROI: {ct.roi:.2%}, Win Rate: {ct.win_rate:.2%}")
            return ct
        finally:
            db.close()

    async def follow_trader(self, address: str) -> CopyTrader | None:
        """Follow a trader from the leaderboard - fetches full stats and activates."""
        return await self.add_trader_with_stats(address)

    def _load_last_positions(self, trader_address: str) -> set[str]:
        """Load last seen positions for a trader from database."""
        db = SessionLocal()
        try:
            states = db.query(TraderPositionState).filter_by(trader_address=trader_address).all()
            return {s.market_id for s in states}
        finally:
            db.close()

    def _save_last_positions(self, trader_address: str, current_ids: set[str]):
        """Save current positions for a trader to database."""
        db = SessionLocal()
        try:
            # Clear old states
            db.query(TraderPositionState).filter_by(trader_address=trader_address).delete()
            # Insert new states
            for market_id in current_ids:
                db.add(TraderPositionState(
                    trader_address=trader_address,
                    market_id=market_id,
                ))
            db.commit()
        finally:
            db.close()

    # ── Trader Discovery ───────────────────────────────────────────────────────

    async def refresh_top_traders(self, limit: int = 20) -> list[dict]:
        """Fetch leaderboard and upsert into DB."""
        traders = await self.client.get_top_traders(limit=limit)
        db = SessionLocal()
        try:
            for t in traders:
                if not t["address"]:
                    continue
                existing = db.query(CopyTrader).filter_by(address=t["address"]).first()
                if existing:
                    existing.roi = t["roi"]
                    existing.total_profit = t["profit"]
                    existing.volume = t["volume"]
                    existing.win_rate = t["win_rate"]
                    if t["name"]:
                        existing.name = t["name"]
                else:
                    db.add(CopyTrader(
                        address=t["address"],
                        name=t["name"] or t["address"][:8] + "...",
                        roi=t["roi"],
                        total_profit=t["profit"],
                        volume=t["volume"],
                        win_rate=t["win_rate"],
                        active=True,
                    ))
            db.commit()
        finally:
            db.close()
        return traders

    # ── Position Polling ───────────────────────────────────────────────────────

    async def poll_and_generate_signals(self) -> list[dict]:
        """Poll all active traders and return new copy signals."""
        db = SessionLocal()
        try:
            active_traders = db.query(CopyTrader).filter_by(active=True).all()
        finally:
            db.close()

        all_signals = []
        for trader in active_traders:
            try:
                signals = await self._check_trader(trader)
                all_signals.extend(signals)
            except Exception as exc:
                logger.warning(f"Error checking trader {trader.address}: {exc}")

        return all_signals

    async def _check_trader(self, trader: CopyTrader) -> list[dict]:
        """Detect new positions opened by a single trader."""
        positions = await self.client.get_trader_positions(trader.address)
        current_ids = {p["market_id"] for p in positions if p["market_id"]}
        prev_ids = self._load_last_positions(trader.address)

        new_market_ids = current_ids - prev_ids
        self._save_last_positions(trader.address, current_ids)

        signals = []
        for pos in positions:
            if pos["market_id"] not in new_market_ids:
                continue
            copied_size = min(pos["size"] * self.scale_factor, self.max_bet)
            if copied_size < 1.0:
                continue

            signal = {
                "trader_address": trader.address,
                "trader_name": trader.name or trader.address[:10],
                "market_id": pos["market_id"],
                "question": pos["question"],
                "side": pos["side"],
                "original_size": pos["size"],
                "copied_size": round(copied_size, 2),
                "price": pos["avg_price"],
                "timestamp": datetime.utcnow().isoformat(),
            }
            signal["_signal_id"] = self._save_signal(signal)
            signals.append(signal)

        return signals

    def _save_signal(self, signal: dict) -> int:
        """Persist a copy signal and return its database ID."""
        db = SessionLocal()
        try:
            cs = CopySignal(
                trader_address=signal["trader_address"],
                trader_name=signal["trader_name"],
                market_id=signal["market_id"],
                question=signal["question"],
                side=signal["side"],
                original_size=signal["original_size"],
                copied_size=signal["copied_size"],
                price=signal["price"],
                status="pending",
            )
            db.add(cs)
            db.commit()
            db.refresh(cs)
            return cs.id
        finally:
            db.close()

    def mark_signal_executed(self, signal_id: int) -> None:
        """Update a signal's status to 'executed' after the trade completes."""
        db = SessionLocal()
        try:
            cs = db.query(CopySignal).filter_by(id=signal_id).first()
            if cs:
                cs.status = "executed"
                db.commit()
        finally:
            db.close()

    # ── Manual Trader Management ───────────────────────────────────────────────

    def add_trader(self, address: str, name: Optional[str] = None) -> CopyTrader:
        """Legacy method - adds trader without fetching stats."""
        db = SessionLocal()
        try:
            existing = db.query(CopyTrader).filter_by(address=address).first()
            if existing:
                existing.active = True
                db.commit()
                return existing
            ct = CopyTrader(
                address=address,
                name=name or address[:10] + "...",
                active=True,
            )
            db.add(ct)
            db.commit()
            db.refresh(ct)
            return ct
        finally:
            db.close()

    def remove_trader(self, trader_id: int) -> bool:
        db = SessionLocal()
        try:
            ct = db.query(CopyTrader).filter_by(id=trader_id).first()
            if not ct:
                return False
            ct.active = False
            db.commit()
            return True
        finally:
            db.close()

    def get_all_traders(self) -> list[dict]:
        db = SessionLocal()
        try:
            traders = db.query(CopyTrader).order_by(CopyTrader.total_profit.desc()).all()
            return [
                {
                    "id": t.id,
                    "address": t.address,
                    "name": t.name,
                    "roi": t.roi,
                    "total_profit": t.total_profit,
                    "volume": t.volume,
                    "win_rate": t.win_rate,
                    "active": t.active,
                    "added_at": t.added_at.isoformat() if t.added_at else None,
                }
                for t in traders
            ]
        finally:
            db.close()

    def get_recent_signals(self, limit: int = 50) -> list[dict]:
        db = SessionLocal()
        try:
            signals = (
                db.query(CopySignal)
                .order_by(CopySignal.timestamp.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": s.id,
                    "trader_address": s.trader_address,
                    "trader_name": s.trader_name,
                    "market_id": s.market_id,
                    "question": s.question,
                    "side": s.side,
                    "original_size": s.original_size,
                    "copied_size": s.copied_size,
                    "price": s.price,
                    "status": s.status,
                    "timestamp": s.timestamp.isoformat() if s.timestamp else None,
                }
                for s in signals
            ]
        finally:
            db.close()
