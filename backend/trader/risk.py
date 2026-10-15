"""backend/trader/risk.py — Position sizing, exposure caps, and drawdown breaker.

Design principles:
- Never bet more than fractional Kelly (usually much less than full Kelly).
- Cap correlated exposure: negRisk events are ONE bet spread across outcomes.
- Circuit-break the whole bot on a bad week instead of compounding losses.
All state (weekly peak equity, halt flag) persists in the StateKV table so a
restart cannot be used to "reset" the breaker.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.database import Position, StateKV

logger = logging.getLogger(__name__)

_UTC = timezone.utc

HALT_KEY = "risk:halt"
WEEK_KEY_PREFIX = "risk:week:"


def _iso_week(dt: datetime) -> str:
    y, w, _ = (dt or datetime.now(_UTC)).isocalendar()
    return f"{y}-W{w:02d}"


def _kv_get(db: Session, key: str) -> dict | None:
    row = db.get(StateKV, key)
    if not row or not row.value:
        return None
    try:
        return json.loads(row.value)
    except json.JSONDecodeError:
        return None


def _kv_set(db: Session, key: str, value: dict):
    row = db.get(StateKV, key)
    if row:
        row.value = json.dumps(value)
        row.updated_at = datetime.now(_UTC).replace(tzinfo=None)
    else:
        db.add(StateKV(key=key, value=json.dumps(value)))
    db.commit()


class RiskManager:
    def __init__(
        self,
        kelly_fraction: float = 0.25,
        max_position_pct: float = 5.0,
        max_open_positions: int = 10,
        max_event_exposure_pct: float = 10.0,
        max_total_exposure_pct: float = 50.0,
        weekly_drawdown_limit_pct: float = 10.0,
        max_spread_edge_ratio: float = 0.5,
        use_kelly_sizing: bool = True,
    ):
        self.kelly_fraction = max(0.0, min(kelly_fraction, 1.0))
        self.max_position_pct = max_position_pct
        self.max_open_positions = max_open_positions
        self.max_event_exposure_pct = max_event_exposure_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.weekly_drawdown_limit_pct = weekly_drawdown_limit_pct
        self.max_spread_edge_ratio = max_spread_edge_ratio
        self.use_kelly_sizing = use_kelly_sizing

    # ── Sizing ─────────────────────────────────────────────────────────────────

    @staticmethod
    def kelly_edge(cost: float, win_prob: float) -> float:
        """Full-Kelly fraction for buying a binary share at `cost` that pays $1
        with probability `win_prob`: f* = (q − c) / (1 − c). 0 if no edge."""
        if not (0.0 < cost < 1.0):
            return 0.0
        q = max(0.0, min(1.0, win_prob))
        f = (q - cost) / (1.0 - cost)
        return max(0.0, f)

    def suggested_size(
        self, cost_price: float, win_prob: float, bankroll: float, max_bet: float,
    ) -> float:
        """Dollar size for a new position: fractional Kelly, capped by per-trade
        percentage of bankroll and the absolute max bet."""
        if bankroll <= 0:
            return 0.0
        f = self.kelly_edge(cost_price, win_prob)
        if not self.use_kelly_sizing or f <= 0:
            # No model probability → fall back to the conservative static cap
            return round(min(max_bet, bankroll * self.max_position_pct / 100.0), 2)
        kelly_bet = bankroll * f * self.kelly_fraction
        cap = bankroll * self.max_position_pct / 100.0
        return round(max(0.0, min(kelly_bet, cap, max_bet)), 2)

    # ── Portfolio snapshots ────────────────────────────────────────────────────

    @staticmethod
    def open_positions(db: Session) -> list[Position]:
        return db.query(Position).all()

    def exposure_summary(self, db: Session, balance: float = 0.0) -> dict:
        positions = self.open_positions(db)
        cost_basis = sum((p.shares or 0) * (p.avg_price or 0) for p in positions)
        market_value = sum((p.shares or 0) * (p.current_price or p.avg_price or 0) for p in positions)
        equity = balance + market_value
        by_event: dict[str, float] = {}
        for p in positions:
            key = p.event_key or p.market_id
            by_event[key] = by_event.get(key, 0.0) + (p.shares or 0) * (p.avg_price or 0)
        return {
            "n_open": len(positions),
            "cost_basis": round(cost_basis, 2),
            "market_value": round(market_value, 2),
            "equity": round(equity, 2),
            "event_exposure": {k: round(v, 2) for k, v in sorted(by_event.items(), key=lambda kv: -kv[1])[:10]},
        }

    # ── Drawdown breaker ───────────────────────────────────────────────────────

    def is_halted(self, db: Session) -> tuple[bool, dict | None]:
        halt = _kv_get(db, HALT_KEY)
        if not halt:
            return False, None
        # Halt is scoped to the week it was raised in; a new week clears it.
        if halt.get("week") == _iso_week(datetime.now(_UTC)):
            return True, halt
        return False, halt

    def update_drawdown_breaker(self, db: Session, equity: float) -> dict:
        """Track this week's peak equity; trip the halt when it drops more than
        the configured percentage from the peak. Returns the breaker status."""
        now = datetime.now(_UTC)
        week = _iso_week(now)
        key = f"{WEEK_KEY_PREFIX}{week}"
        state = _kv_get(db, key) or {"peak": equity}
        state["peak"] = max(state.get("peak", equity), equity)
        state["updated"] = now.isoformat()
        _kv_set(db, key, state)

        peak = state["peak"]
        drawdown_pct = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
        halted, halt_info = self.is_halted(db)

        if drawdown_pct >= self.weekly_drawdown_limit_pct and not halted:
            _kv_set(db, HALT_KEY, {
                "week": week,
                "peak": round(peak, 2),
                "equity": round(equity, 2),
                "drawdown_pct": round(drawdown_pct, 2),
                "reason": f"Weekly drawdown {drawdown_pct:.1f}% >= limit {self.weekly_drawdown_limit_pct}%",
            })
            halted = True
            logger.warning(f"RISK BREAKER TRIPPED: {_kv_get(db, HALT_KEY)['reason']}")

        return {
            "week": week,
            "peak_equity": round(peak, 2),
            "equity": round(equity, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "limit_pct": self.weekly_drawdown_limit_pct,
            "halted": halted,
            "halted_at": halt_info.get("reason") if halt_info else None,
        }

    def clear_halt(self, db: Session):
        row = db.get(StateKV, HALT_KEY)
        if row:
            db.delete(row)
            db.commit()

    # ── Pre-trade checks ───────────────────────────────────────────────────────

    def check_trade(
        self,
        db: Session,
        size_usd: float,
        market_id: str,
        event_key: str | None = None,
        balance: float = 0.0,
        spread: float | None = None,
        edge_pct: float | None = None,
    ) -> tuple[bool, str, float]:
        """Returns (allowed, reason, approved_size). approved_size may be shrunk
        to fit an exposure cap rather than rejecting outright."""
        halted, halt_info = self.is_halted(db)
        if halted:
            return False, f"Trading halted: {halt_info.get('reason', 'drawdown breaker tripped')}", 0.0

        # Spread/edge gate: a wide book means the edge is theoretical, not executable
        if spread is not None and edge_pct is not None and edge_pct > 0:
            spread_pct = spread * 100.0
            if spread_pct > self.max_spread_edge_ratio * edge_pct:
                return False, (
                    f"Spread {spread_pct:.1f}% too wide vs edge {edge_pct:.1f}% "
                    f"(max ratio {self.max_spread_edge_ratio:.2f})"
                ), 0.0

        summary = self.exposure_summary(db, balance)
        bankroll = max(summary["equity"], 1e-9)

        positions = self.open_positions(db)
        existing_market = any(p.market_id == market_id for p in positions)
        if not existing_market and len(positions) >= self.max_open_positions:
            return False, f"Max open positions ({self.max_open_positions}) reached", 0.0

        # Total exposure cap
        total_cap = bankroll * self.max_total_exposure_pct / 100.0
        headroom_total = total_cap - summary["cost_basis"]
        if headroom_total <= 0:
            return False, f"Total exposure cap ${total_cap:.0f} already used", 0.0

        # Per-event (correlated group) cap — negRisk markets share one event_key
        key = event_key or market_id
        event_used = summary["event_exposure"].get(key, 0.0)
        event_cap = bankroll * self.max_event_exposure_pct / 100.0
        headroom_event = event_cap - event_used
        if headroom_event <= 0:
            return False, f"Event exposure cap ${event_cap:.0f} used for {key[:16]}…", 0.0

        approved = round(min(size_usd, headroom_total, headroom_event), 2)
        if approved < 1.0:
            return False, f"Only ${approved:.2f} headroom left under exposure caps", 0.0
        reason = "ok"
        if approved < size_usd:
            reason = f"Size capped from ${size_usd:.2f} to ${approved:.2f} by exposure limits"
            if abs(approved - size_usd) / max(size_usd, 1e-9) > 0.5:
                # Half the bet evaporating usually means the setup is bad anyway
                return False, f"Exposure headroom (<50% of requested size) for {key[:16]}…", 0.0
        return True, reason, approved
