"""backend/trader/executor.py — Order execution (paper + live)."""

import logging
from datetime import datetime, date, timezone

from sqlalchemy.orm import Session

from backend.database import Trade, Position, SessionLocal

logger = logging.getLogger(__name__)

_UTC = timezone.utc


class TradeExecutor:
    """Executes trades in paper or live mode and persists to DB."""

    def __init__(self, paper_mode: bool = True, client=None, paper_balance: float = 10000.0, max_daily_spend: float = 0.0):
        self.paper_mode = paper_mode
        self.client = client  # PolymarketClient, required for live mode
        self._paper_balance = paper_balance
        self.max_daily_spend = max_daily_spend

    # ── Public Interface ───────────────────────────────────────────────────────

    async def execute(
        self,
        market_id: str,
        question: str,
        side: str,
        size_usd: float,
        price: float,
        source: str = "manual",
        model_used: str = None,
    ) -> dict:
        """Execute a trade. Returns a trade record dict."""
        side = side.upper()

        if not self._check_daily_limit(size_usd, self.max_daily_spend):
            logger.warning(f"Trade rejected: would exceed daily spend limit of ${self.max_daily_spend}")
            return {
                "market_id": market_id,
                "question": question,
                "side": side,
                "size": size_usd,
                "price": price,
                "status": "rejected",
                "mode": "paper" if self.paper_mode else "live",
                "source": source,
                "error": f"Daily spend limit of ${self.max_daily_spend} would be exceeded",
            }

        if self.paper_mode:
            return await self._paper_trade(market_id, question, side, size_usd, price, source, model_used)
        else:
            return await self._live_trade(market_id, question, side, size_usd, price, source, model_used)

    # ── Paper Mode ─────────────────────────────────────────────────────────────

    async def _paper_trade(self, market_id, question, side, size_usd, price, source, model_used) -> dict:
        if self._paper_balance <= 0:
            logger.warning("Paper balance is zero — cannot execute trade")
            return {
                "market_id": market_id, "question": question, "side": side,
                "size": size_usd, "price": price, "status": "rejected",
                "mode": "paper", "source": source,
                "error": "Paper balance is zero",
            }

        size_usd = min(size_usd, self._paper_balance)
        shares = round(size_usd / price, 4) if price > 0 else 0

        db = SessionLocal()
        try:
            opposite_side = "NO" if side == "YES" else "YES"
            pnl = self._db_close_opposite(db, market_id, opposite_side, price)
            self._db_update_position(db, market_id, question, side, shares, price, "paper", source)
            trade = self._db_save_trade(
                db, market_id, question, side, size_usd, price, shares,
                "filled", "paper", source, model_used, pnl,
            )
            db.commit()
            self._paper_balance -= size_usd
            logger.info(
                f"[PAPER] {side} ${size_usd:.2f} @ {price:.3f} on '{question[:50]}'"
                + (f" (closed opposite PnL: ${pnl:.2f})" if pnl else "")
            )
            return trade
        except Exception as exc:
            db.rollback()
            logger.error(f"Paper trade DB failed: {exc}")
            raise
        finally:
            db.close()

    # ── Live Mode ──────────────────────────────────────────────────────────────

    async def _live_trade(self, market_id, question, side, size_usd, price, source, model_used) -> dict:
        if not self.client:
            raise RuntimeError("PolymarketClient required for live trading")

        filled_price = price
        shares = 0.0
        status = "failed"
        pnl = None

        try:
            result = await self.client.place_market_order(market_id, side, size_usd)
            filled_price = float(result.get("price", price))
            shares = float(result.get("shares", size_usd / filled_price if filled_price > 0 else 0))
            status = "filled"
        except Exception as exc:
            logger.error(f"Live order placement failed: {exc}")

        db = SessionLocal()
        try:
            if status == "filled":
                opposite_side = "NO" if side == "YES" else "YES"
                pnl = self._db_close_opposite(db, market_id, opposite_side, filled_price)
                self._db_update_position(db, market_id, question, side, shares, filled_price, "live", source)

            trade = self._db_save_trade(
                db, market_id, question, side, size_usd, filled_price, shares,
                status, "live", source, model_used, pnl,
            )
            db.commit()
            logger.info(
                f"[LIVE] {side} ${size_usd:.2f} @ {filled_price:.3f} — status: {status}"
                + (f" (closed opposite PnL: ${pnl:.2f})" if pnl and status == "filled" else "")
            )
            return trade
        except Exception as exc:
            db.rollback()
            logger.error(f"Live trade DB save failed: {exc}")
            return {
                "market_id": market_id, "question": question, "side": side,
                "size": size_usd, "price": filled_price, "shares": shares,
                "status": "failed", "mode": "live", "source": source,
                "model_used": model_used, "pnl": None,
                "timestamp": datetime.now(_UTC).isoformat(),
                "error": str(exc),
            }
        finally:
            db.close()

    # ── DB Helpers (all accept an open Session for single-transaction use) ─────

    def _db_close_opposite(self, db: Session, market_id: str, opposite_side: str, exit_price: float) -> float:
        """Fully close the opposite-side position, returning realized PnL.

        In binary prediction markets, holding YES and NO simultaneously is contradictory,
        so we always close the entire opposite position rather than partial shares.
        """
        pos = db.query(Position).filter_by(market_id=market_id, side=opposite_side).first()
        if not pos or pos.shares <= 0:
            return 0.0
        pnl = round((exit_price - pos.avg_price) * pos.shares, 4)
        logger.debug(
            f"Closing {pos.shares:.4f} {opposite_side} shares on {market_id[:12]}… "
            f"avg_price={pos.avg_price:.4f} exit={exit_price:.4f} pnl={pnl:.2f}"
        )
        db.delete(pos)
        return pnl

    def _db_update_position(
        self, db: Session, market_id: str, question: str, side: str,
        new_shares: float, price: float, mode: str, source: str,
    ) -> None:
        """Add to or create the position, updating avg_price and unrealized PnL."""
        pos = db.query(Position).filter_by(market_id=market_id, side=side).first()
        if pos:
            total = pos.shares + new_shares
            pos.avg_price = (
                (pos.avg_price * pos.shares + price * new_shares) / total
                if total else price
            )
            pos.shares = total
            pos.current_price = price
            pos.pnl = round((pos.current_price - pos.avg_price) * pos.shares, 4)
            pos.updated_at = datetime.now(_UTC)
        else:
            db.add(Position(
                market_id=market_id,
                question=question,
                side=side,
                shares=new_shares,
                avg_price=price,
                current_price=price,
                pnl=0.0,
                mode=mode,
                source=source,
            ))

    def _db_save_trade(
        self, db: Session, market_id: str, question: str, side: str,
        size: float, price: float, shares: float, status: str,
        mode: str, source: str, model_used: str | None, pnl: float | None,
    ) -> dict:
        """Insert a trade record and return its dict (uses db.flush() to get id)."""
        trade = Trade(
            market_id=market_id,
            question=question,
            side=side,
            size=size,
            price=price,
            shares=shares,
            status=status,
            mode=mode,
            source=source,
            model_used=model_used,
            pnl=pnl,
        )
        db.add(trade)
        db.flush()  # Assigns trade.id without committing
        return {
            "id": trade.id,
            "market_id": trade.market_id,
            "question": trade.question,
            "side": trade.side,
            "size": trade.size,
            "price": trade.price,
            "shares": trade.shares,
            "status": trade.status,
            "mode": trade.mode,
            "source": trade.source,
            "model_used": trade.model_used,
            "pnl": trade.pnl,
            "timestamp": datetime.now(_UTC).isoformat(),
        }

    def _get_daily_spend(self) -> float:
        """Get total USD spent today (UTC)."""
        db = SessionLocal()
        try:
            today_utc = datetime.now(_UTC).date()
            day_start = datetime(today_utc.year, today_utc.month, today_utc.day, tzinfo=_UTC)
            trades = db.query(Trade).filter(
                Trade.status == "filled",
                Trade.timestamp >= day_start.replace(tzinfo=None),  # DB stores naive UTC
            ).all()
            return sum(t.size for t in trades)
        finally:
            db.close()

    def _check_daily_limit(self, size_usd: float, max_daily_spend: float) -> bool:
        if max_daily_spend <= 0:
            return True
        return (self._get_daily_spend() + size_usd) <= max_daily_spend

    @property
    def paper_balance(self) -> float:
        return self._paper_balance
