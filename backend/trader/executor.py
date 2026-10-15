"""backend/trader/executor.py — Order execution (paper + live)."""

import logging
from datetime import datetime, date, timezone

from sqlalchemy.orm import Session

from backend.database import Trade, Position, SessionLocal

logger = logging.getLogger(__name__)

_UTC = timezone.utc


class TradeExecutor:
    """Executes trades in paper or live mode and persists to DB."""

    def __init__(self, paper_mode: bool = True, client=None, paper_balance: float = 10000.0, max_daily_spend: float = 0.0, risk_manager=None):
        self.paper_mode = paper_mode
        self.client = client  # PolymarketClient, required for live mode
        self._paper_balance = paper_balance
        self.max_daily_spend = max_daily_spend
        self.risk = risk_manager  # backend.trader.risk.RiskManager or None

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
        token_id: str = "",
        est_prob: float | None = None,
        event_key: str = "",
        spread: float | None = None,
        edge_pct: float | None = None,
    ) -> dict:
        """Execute a trade. Returns a trade record dict.

        `token_id` is the CLOB token id for the outcome being bought; live mode
        requires it (falls back to on-chain resolution if not supplied).
        `est_prob` is the model's YES probability (calibration + monitoring),
        `event_key` groups correlated (negRisk) markets for exposure caps.
        """
        side = side.upper()

        if not self._check_daily_limit(size_usd, self.max_daily_spend):
            logger.warning(f"Trade rejected: would exceed daily spend limit of ${self.max_daily_spend}")
            return self._reject_dict(market_id, question, side, size_usd, price,
                                     f"Daily spend limit of ${self.max_daily_spend} would be exceeded", source=source)

        # Risk layer: halt flag, spread/edge gate, position count + exposure caps.
        # May approve a smaller size instead of rejecting.
        if self.risk:
            db = SessionLocal()
            try:
                allowed, reason, size_usd = self.risk.check_trade(
                    db, size_usd, market_id, event_key or None,
                    balance=self._paper_balance if self.paper_mode else 0.0,
                    spread=spread, edge_pct=edge_pct,
                )
            finally:
                db.close()
            if not allowed:
                logger.warning(f"Trade rejected by risk layer: {reason}")
                return self._reject_dict(market_id, question, side, size_usd, price, reason, source=source)

        if self.paper_mode:
            return await self._paper_trade(market_id, question, side, size_usd, price, source, model_used, est_prob, event_key)
        else:
            return await self._live_trade(market_id, question, side, size_usd, price, source, model_used, token_id, est_prob, event_key)

    def _reject_dict(self, market_id, question, side, size, price, error, source: str = "manual") -> dict:
        return {
            "market_id": market_id, "question": question, "side": side,
            "size": size, "price": price, "status": "rejected",
            "mode": "paper" if self.paper_mode else "live",
            "source": source, "error": error,
        }

    # ── Paper Mode ─────────────────────────────────────────────────────────────

    async def _paper_trade(self, market_id, question, side, size_usd, price, source, model_used, est_prob=None, event_key="") -> dict:
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
            pnl, proceeds = self._db_close_opposite(db, market_id, opposite_side, price)
            # Flush deletes before the potential insert on the same market_id
            # (Position.market_id is UNIQUE and SQLAlchemy would insert first otherwise).
            db.flush()
            self._db_update_position(db, market_id, question, side, shares, price, "paper", source, est_prob=est_prob, event_key=event_key)
            trade = self._db_save_trade(
                db, market_id, question, side, size_usd, price, shares,
                "filled", "paper", source, model_used, pnl,
            )
            db.commit()
            self._paper_balance = self._paper_balance - size_usd + proceeds
            logger.info(
                f"[PAPER] {side} ${size_usd:.2f} @ {price:.3f} on '{question[:50]}'"
                + (f" (closed opposite PnL: ${pnl:.2f}, proceeds: ${proceeds:.2f})" if pnl or proceeds else "")
            )
            return trade
        except Exception as exc:
            db.rollback()
            logger.error(f"Paper trade DB failed: {exc}")
            raise
        finally:
            db.close()

    # ── Live Mode ──────────────────────────────────────────────────────────────

    async def _live_trade(self, market_id, question, side, size_usd, price, source, model_used, token_id="", est_prob=None, event_key="") -> dict:
        if not self.client:
            raise RuntimeError("PolymarketClient required for live trading")

        filled_price = price
        shares = 0.0
        status = "failed"
        pnl = None
        error: str | None = None

        try:
            # Live orders trade the outcome TOKEN, not the market condition id.
            if not token_id:
                token_id = await self.client.resolve_token_id(market_id, side)
            # Refresh the executable ask right before ordering (stale UI prices slip).
            exec_price = await self.client.get_token_price(token_id, "buy")
            if exec_price and 0 < exec_price < 1:
                filled_price = exec_price
            result = await self.client.place_market_order(token_id, side, size_usd)
            filled_price = float(result.get("price", filled_price))
            shares = float(result.get("shares", size_usd / filled_price if filled_price > 0 else 0))
            status = "filled"
        except Exception as exc:
            error = str(exc)
            logger.error(f"Live order placement failed: {exc}")

        db = SessionLocal()
        try:
            proceeds = 0.0
            if status == "filled":
                opposite_side = "NO" if side == "YES" else "YES"
                pnl, proceeds = self._db_close_opposite(db, market_id, opposite_side, filled_price)
                db.flush()  # execute delete before same-market insert (unique market_id)
                self._db_update_position(db, market_id, question, side, shares, filled_price, "live", source, token_id=token_id, est_prob=est_prob, event_key=event_key)

            trade = self._db_save_trade(
                db, market_id, question, side, size_usd, filled_price, shares,
                status, "live", source, model_used, pnl,
            )
            db.commit()
            logger.info(
                f"[LIVE] {side} ${size_usd:.2f} @ {filled_price:.3f} — status: {status}"
                + (f" (closed opposite PnL: ${pnl:.2f})" if pnl and status == "filled" else "")
            )
            if error and status != "filled":
                trade["error"] = error
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

    # ── Position Exit (used by the monitor loop) ──────────────────────────────

    async def close_position(self, market_id: str, side: str, exit_price: float, reason: str = "") -> dict:
        """Sell out of an entire open position (paper or live) and record the exit.

        Live mode sells the held outcome token via the SDK; if the live order
        fails the DB position is left untouched so nothing is "closed" silently.
        """
        side = side.upper()
        db = SessionLocal()
        try:
            pos = db.query(Position).filter_by(market_id=market_id, side=side).first()
            if not pos or pos.shares <= 0:
                return {"status": "noop", "market_id": market_id, "side": side}
            exit_price = max(0.001, min(0.999, exit_price))

            if not self.paper_mode:
                if not self.client:
                    raise RuntimeError("PolymarketClient required for live exits")
                token_id = pos.token_id or await self.client.resolve_token_id(market_id, side)
                # SDK market SELL amount is share-denominated for the outcome token
                await self.client.place_market_order(token_id, "SELL", pos.shares)

            proceeds = round(exit_price * pos.shares, 4)
            pnl = round((exit_price - pos.avg_price) * pos.shares, 4)
            mode = pos.mode
            trade = self._db_save_trade(
                db, market_id, pos.question, side, proceeds, exit_price, pos.shares,
                "closed", mode, "exit", None, pnl,
            )
            db.delete(pos)
            db.commit()
            if mode == "paper":
                self._paper_balance += proceeds
            logger.info(
                f"[EXIT {mode.upper()}] sold {pos.shares:.1f} {side} @ {exit_price:.3f} "
                f"on '{pos.question[:48]}' pnl={pnl:+.2f} ({reason})"
            )
            trade["reason"] = reason
            trade["pnl"] = pnl
            return trade
        except Exception as exc:
            db.rollback()
            logger.error(f"Position close failed for {market_id[:12]}… {side}: {exc}")
            return {"status": "failed", "market_id": market_id, "side": side, "error": str(exc)}
        finally:
            db.close()

    # ── DB Helpers (all accept an open Session for single-transaction use) ─────

    def _db_close_opposite(
        self, db: Session, market_id: str, opposite_side: str, trade_price: float,
    ) -> tuple[float, float]:
        """Fully close the opposite-side position.

        Returns (realized_pnl, sale_proceeds_usd).

        In binary prediction markets, holding YES and NO simultaneously is
        contradictory, so we always close the entire opposite position.

        PnL math: `trade_price` is the price paid for the NEW side, so the
        opposite side's exit price is `1 - trade_price` (YES and NO are
        complementary: P_yes + P_no ≈ 1). Selling the old NOs at 1 − YES price
        is what the CLOB would actually pay.
        """
        pos = db.query(Position).filter_by(market_id=market_id, side=opposite_side).first()
        if not pos or pos.shares <= 0:
            return 0.0, 0.0
        exit_price = max(0.0, min(1.0, 1.0 - trade_price))
        pnl = round((exit_price - pos.avg_price) * pos.shares, 4)
        proceeds = round(exit_price * pos.shares, 4)
        logger.debug(
            f"Closing {pos.shares:.4f} {opposite_side} shares on {market_id[:12]}… "
            f"avg_price={pos.avg_price:.4f} exit={exit_price:.4f} pnl={pnl:.2f} proceeds={proceeds:.2f}"
        )
        db.delete(pos)
        return pnl, proceeds

    def _db_update_position(
        self, db: Session, market_id: str, question: str, side: str,
        new_shares: float, price: float, mode: str, source: str,
        token_id: str = "", est_prob: float | None = None, event_key: str = "",
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
            if token_id and not pos.token_id:
                pos.token_id = token_id
            if event_key and not pos.event_key:
                pos.event_key = event_key
            if est_prob is not None and pos.est_prob is None:
                pos.est_prob = est_prob
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
                token_id=token_id or None,
                event_key=event_key or None,
                est_prob=est_prob,
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
