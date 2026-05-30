"""backend/trader/portfolio.py — Portfolio tracking + P&L computation."""

from datetime import datetime, timedelta

from sqlalchemy import func, case

from backend.database import Trade, Position, SessionLocal


class Portfolio:
    """Computes portfolio metrics from the trade/position database."""

    def get_summary(self, paper_balance: float = 0.0) -> dict:
        db = SessionLocal()
        try:
            # Aggregate at the SQL level — avoids loading all rows into memory
            result = db.query(
                func.coalesce(func.sum(Trade.size), 0.0).label("total_invested"),
                func.coalesce(func.sum(Trade.pnl), 0.0).label("total_pnl"),
                func.count(Trade.id).label("total_trades"),
                func.sum(case((Trade.pnl > 0, 1), else_=0)).label("wins"),
            ).filter(Trade.status == "filled").one()

            total_trades = result.total_trades or 0
            wins = result.wins or 0
            win_rate = (wins / total_trades * 100) if total_trades else 0
            open_positions = db.query(func.count(Position.id)).scalar() or 0

            return {
                "paper_balance": round(paper_balance, 2),
                "total_invested": round(float(result.total_invested), 2),
                "total_pnl": round(float(result.total_pnl), 2),
                "total_trades": total_trades,
                "wins": wins,
                "losses": total_trades - wins,
                "win_rate": round(win_rate, 1),
                "open_positions": open_positions,
            }
        finally:
            db.close()

    def get_positions(self) -> list[dict]:
        db = SessionLocal()
        try:
            positions = db.query(Position).order_by(Position.opened_at.desc()).all()
            return [
                {
                    "id": p.id,
                    "market_id": p.market_id,
                    "question": p.question,
                    "side": p.side,
                    "shares": p.shares,
                    "avg_price": p.avg_price,
                    "current_price": p.current_price,
                    "pnl": round(p.pnl or 0, 2),
                    "mode": p.mode,
                    "source": p.source,
                    "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                }
                for p in positions
            ]
        finally:
            db.close()

    def get_trades(self, limit: int = 100, source: str = None) -> list[dict]:
        db = SessionLocal()
        try:
            q = db.query(Trade).order_by(Trade.timestamp.desc())
            if source:
                q = q.filter(Trade.source == source)
            trades = q.limit(limit).all()
            return [
                {
                    "id": t.id,
                    "market_id": t.market_id,
                    "question": t.question,
                    "side": t.side,
                    "size": t.size,
                    "price": t.price,
                    "shares": t.shares,
                    "status": t.status,
                    "pnl": round(t.pnl or 0, 2),
                    "mode": t.mode,
                    "source": t.source,
                    "model_used": t.model_used,
                    "timestamp": t.timestamp.isoformat() if t.timestamp else None,
                }
                for t in trades
            ]
        finally:
            db.close()

    def get_daily_pnl(self, days: int = 30) -> list[dict]:
        """Return daily P&L for charting, aggregated in SQL."""
        db = SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(days=days)
            rows = (
                db.query(
                    func.date(Trade.timestamp).label("day"),
                    func.coalesce(func.sum(Trade.pnl), 0.0).label("pnl"),
                )
                .filter(Trade.status == "filled", Trade.timestamp >= cutoff)
                .group_by(func.date(Trade.timestamp))
                .order_by(func.date(Trade.timestamp))
                .all()
            )
            return [{"date": str(r.day), "pnl": round(float(r.pnl), 2)} for r in rows]
        finally:
            db.close()
