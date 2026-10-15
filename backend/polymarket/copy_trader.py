"""backend/polymarket/copy_trader.py — Copy trading engine."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.orm import Session

from backend.polymarket.client import PolymarketClient
from backend.database import CopyTrader, CopySignal, TraderPositionState, SessionLocal

logger = logging.getLogger(__name__)

_UTC = timezone.utc
_SIGNAL_DEDUPE_HOURS = 24  # skip re-signaling the same (trader, market, side) within this window
_MAX_CONCURRENT_POLLS = 4


def _to_ts(value) -> Optional[int]:
    """Normalize a v2 position timestamp (unix int or ISO string) to unix seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = int(value)
        # Some routes return milliseconds — normalize to seconds
        return ts // 1000 if ts > 10_000_000_000 else ts
    s = str(value).strip()
    if s.isdigit():
        return _to_ts(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        return int(dt.timestamp())
    except Exception:
        return None


class CopyTradingEngine:
    """Monitors top traders and generates copy signals."""

    def __init__(self, client: PolymarketClient, scale_factor: float = 0.1, max_bet: float = 100.0):
        self.client = client
        self.scale_factor = scale_factor
        self.max_bet = max_bet

    async def add_trader_with_stats(self, address: str, name: Optional[str] = None) -> CopyTrader | None:
        """Add a trader and fetch their stats from the Data API v2."""
        stats = await self.client.get_trader_stats(address)
        win_rate = 0.0
        if stats:
            win_rate = stats.get("win_rate", 0) or 0
        if not win_rate:
            # v2 user-stats has no win rate — estimate from closed positions
            est = await self.client.get_trader_win_rate(address)
            if est is not None:
                win_rate = est

        db = SessionLocal()
        try:
            existing = db.query(CopyTrader).filter_by(address=address).first()
            if existing:
                existing.active = True
                if stats:
                    existing.roi = stats.get("roi", existing.roi or 0)
                    existing.total_profit = stats.get("profit", existing.total_profit or 0)
                    existing.volume = stats.get("volume", existing.volume or 0)
                    existing.win_rate = win_rate or existing.win_rate or 0
                    if stats.get("name"):
                        existing.name = stats["name"]
                db.commit()
                return existing

            ct = CopyTrader(
                address=address,
                name=name or (stats.get("name") if stats else None) or address[:10] + "...",
                roi=stats.get("roi", 0) if stats else 0,
                total_profit=stats.get("profit", 0) if stats else 0,
                volume=stats.get("volume", 0) if stats else 0,
                win_rate=win_rate,
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

    # ── Position state (survives restarts) ─────────────────────────────────────

    def _load_last_positions(self, trader_address: str) -> dict[str, dict]:
        """Load last seen positions for a trader: market_id -> {side, last_event_ts}."""
        db = SessionLocal()
        try:
            states = db.query(TraderPositionState).filter_by(trader_address=trader_address).all()
            return {
                s.market_id: {"side": (s.side or "").upper(), "ts": s.last_event_ts}
                for s in states
            }
        finally:
            db.close()

    def _save_last_positions(self, trader_address: str, positions: list[dict]):
        """Persist current positions (side + last-event watermark) for a trader."""
        db = SessionLocal()
        try:
            db.query(TraderPositionState).filter_by(trader_address=trader_address).delete()
            for p in positions:
                if not p.get("market_id"):
                    continue
                db.add(TraderPositionState(
                    trader_address=trader_address,
                    market_id=p["market_id"],
                    side=(p.get("side") or "").upper(),
                    last_event_ts=_to_ts(p.get("last_event_at")),
                ))
            db.commit()
        finally:
            db.close()

    def _mark_polled(self, trader_address: str):
        """Advance the first-poll watermark so a restart doesn't re-flood signals."""
        db = SessionLocal()
        try:
            ct = db.query(CopyTrader).filter_by(address=trader_address).first()
            if ct:
                ct.last_position_poll = datetime.now(_UTC).replace(tzinfo=None)
                db.commit()
        finally:
            db.close()

    def _recently_signaled(self, db: Session, trader_address: str, market_id: str, side: str) -> bool:
        """True if this exact signal was already emitted within the dedupe window."""
        cutoff = datetime.now(_UTC).replace(tzinfo=None) - timedelta(hours=_SIGNAL_DEDUPE_HOURS)
        return db.query(CopySignal).filter(
            CopySignal.trader_address == trader_address,
            CopySignal.market_id == market_id,
            CopySignal.side == side,
            CopySignal.timestamp >= cutoff,
        ).first() is not None

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
                    if t.get("win_rate"):
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
                        win_rate=t.get("win_rate", 0),
                        active=True,
                    ))
            db.commit()
        finally:
            db.close()
        return traders

    # ── Position Polling ───────────────────────────────────────────────────────

    async def poll_and_generate_signals(self) -> list[dict]:
        """Poll all active traders concurrently and return new copy signals."""
        db = SessionLocal()
        try:
            active_traders = db.query(CopyTrader).filter_by(active=True).all()
            snapshot = [
                {"address": t.address, "name": t.name, "last_position_poll": t.last_position_poll}
                for t in active_traders
            ]
        finally:
            db.close()

        sem = asyncio.Semaphore(_MAX_CONCURRENT_POLLS)

        async def _check(entry: dict) -> list[dict]:
            async with sem:
                try:
                    return await self._check_trader(entry)
                except Exception as exc:
                    logger.warning(f"Error checking trader {entry['address']}: {exc}")
                    return []

        results = await asyncio.gather(*(_check(e) for e in snapshot))
        return [s for signals in results for s in signals]

    async def _check_trader(self, trader: dict) -> list[dict]:
        """Detect new/added positions for a single trader.

        Detection rules:
        - First poll ever (no watermark, no stored state): seed state, emit nothing.
          This prevents a restart from flooding the queue with the trader's whole
          back-catalog of open positions.
        - New market_id in state -> candidate signal.
        - Known market whose last_event_ts advanced or side flipped -> the trader
          added to / changed the position -> candidate signal.
        - Any (trader, market, side) signaled in the last 24h is deduped.
        """
        address = trader["address"]
        positions = await self.client.get_trader_positions(address)
        positions = [p for p in positions if p.get("market_id")]
        prev = self._load_last_positions(address)

        first_poll = trader.get("last_position_poll") is None and not prev

        candidates: list[dict] = []
        if not first_poll:
            for pos in positions:
                state = prev.get(pos["market_id"])
                ts = _to_ts(pos.get("last_event_at"))
                is_new = state is None
                is_flip = state is not None and state.get("side") and pos.get("side") and state["side"] != pos["side"]
                is_add = state is not None and ts is not None and state.get("ts") is not None and ts > state["ts"]
                if is_new or is_flip or is_add:
                    candidates.append(pos)

        self._save_last_positions(address, positions)
        self._mark_polled(address)

        if first_poll:
            logger.info(f"Seeded position state for {address[:10]}… ({len(positions)} open positions, signals suppressed)")
            return []
        if not candidates:
            return []

        signals = []
        db = SessionLocal()
        try:
            for pos in candidates:
                side = (pos.get("side") or "").upper()
                if side not in ("YES", "NO"):
                    continue
                copied_size = min(pos["size"] * self.scale_factor, self.max_bet)
                if copied_size < 1.0:
                    continue
                if self._recently_signaled(db, address, pos["market_id"], side):
                    logger.debug(f"Deduped copy signal for {address[:10]}… {pos['market_id'][:12]}… {side}")
                    continue

                signal = {
                    "trader_address": address,
                    "trader_name": trader.get("name") or address[:10],
                    "market_id": pos["market_id"],
                    "token_id": pos.get("token_id", ""),  # CLOB token — lets live orders skip resolution
                    "question": pos["question"],
                    "side": side,
                    "original_size": pos["size"],
                    "copied_size": round(copied_size, 2),
                    "price": pos["avg_price"],
                    "timestamp": datetime.now(_UTC).isoformat(),
                }
                signal["_signal_id"] = self._save_signal(db, signal)
                signals.append(signal)
        finally:
            db.close()

        return signals

    def _save_signal(self, db: Session, signal: dict) -> int:
        """Persist a copy signal and return its database ID."""
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
