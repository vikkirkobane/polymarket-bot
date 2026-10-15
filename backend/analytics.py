"""backend/analytics.py — Calibration & PnL attribution.

Answers the two questions a trading bot must never guess at:
1. CALIBRATION — when the model says 70%, does the event happen 70% of the time?
   (uses manually marked Win/Loss outcomes against estimated_prob)
2. ATTRIBUTION — which brain (research / copy / consensus) and which model
   actually makes money? Kill what loses.
"""

import logging
from collections import defaultdict

from sqlalchemy.orm import Session

from backend.database import MarketOpportunity, Trade

logger = logging.getLogger(__name__)

_DECIBUCKETS = [round(i / 10, 1) for i in range(10)]  # 0.0 … 0.9


def _bucket(p: float) -> float:
    for lo in reversed(_DECIBUCKETS):
        if p >= lo:
            return lo
    return 0.0


def get_calibration(db: Session, min_side: str = "") -> dict:
    """Bucket marked outcomes by the model's predicted probability of the
    recommended side winning. Deviation from the diagonal IS the edge problem."""
    rows = (
        db.query(MarketOpportunity)
        .filter(
            MarketOpportunity.estimated_prob.isnot(None),
            MarketOpportunity.outcome.isnot(None),
        )
        .all()
    )
    buckets: dict[float, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "sum_pred": 0.0})
    for r in rows:
        est = r.estimated_prob
        if est is None:
            continue
        side = (r.side or "YES").upper()
        # probability that the side WE bet on wins
        p_side = est if side == "YES" else 1.0 - est
        b = buckets[_bucket(p_side)]
        b["n"] += 1
        b["sum_pred"] += p_side
        if r.outcome == "success":
            b["wins"] += 1

    out = []
    for lo in _DECIBUCKETS:
        b = buckets.get(lo)
        if not b or b["n"] == 0:
            continue
        out.append({
            "bucket": f"{lo:.1f}–{lo + 0.1:.1f}",
            "n": b["n"],
            "predicted_avg": round(b["sum_pred"] / b["n"], 3),
            "actual_win_rate": round(b["wins"] / b["n"], 3),
            "gap": round(b["wins"] / b["n"] - b["sum_pred"] / b["n"], 3),
        })
    total_n = sum(x["n"] for x in out)
    overall_gap = (
        sum((x["actual_win_rate"] - x["predicted_avg"]) * x["n"] for x in out) / total_n
        if total_n else 0.0
    )
    return {
        "rows": out,
        "sampled": total_n,
        "avg_gap": round(overall_gap, 3),  # >0 model underconfident, <0 overconfident
        "interpretation": (
            "Not enough outcome history yet — mark picks Win/Loss on the dashboard"
            if total_n < 10 else
            "Model overconfident (actual < predicted) — consider shrinking estimates"
            if overall_gap < -0.08 else
            "Model underconfident (actual > predicted) — edge may be larger than measured"
            if overall_gap > 0.08 else
            "Calibration looks reasonable"
        ),
    }


def get_attribution(db: Session) -> dict:
    """Realized PnL and hit-rate grouped by trade source and by AI model."""
    rows = (
        db.query(Trade.source, Trade.model_used, Trade.status, Trade.pnl)
        .filter(Trade.status.in_(["filled", "closed"]))
        .all()
    )

    def _agg():
        return {"trades": 0, "invested": 0.0, "resolved": 0, "wins": 0, "realized_pnl": 0.0}

    by_source: dict[str, dict] = defaultdict(_agg)
    by_model: dict[str, dict] = defaultdict(_agg)
    for source, model, status, pnl in rows:
        for key, table in ((source or "unknown", by_source), (model or "n/a", by_model)):
            cell = table[key]
            cell["trades"] += 1
            if pnl is not None:
                cell["resolved"] += 1
                cell["realized_pnl"] += pnl
                if pnl > 0:
                    cell["wins"] += 1

    # invested per source, one aggregate query
    from sqlalchemy import func
    db_rows = (
        db.query(Trade.source, func.coalesce(func.sum(Trade.size), 0.0))
        .filter(Trade.status == "filled")
        .group_by(Trade.source)
        .all()
    )
    invested = {s or "unknown": float(v) for s, v in db_rows}

    def _finalize(table: dict[str, dict]) -> list[dict]:
        out = []
        for key, cell in table.items():
            resolved = cell["resolved"]
            out.append({
                "name": key,
                "trades": cell["trades"],
                "invested": round(invested.get(key, 0.0), 2) if table is by_source else None,
                "resolved": resolved,
                "win_rate": round(cell["wins"] / resolved * 100, 1) if resolved else None,
                "realized_pnl": round(cell["realized_pnl"], 2),
                "avg_pnl_per_resolved": round(cell["realized_pnl"] / resolved, 2) if resolved else None,
            })
        out.sort(key=lambda x: x["realized_pnl"], reverse=True)
        return out

    return {
        "by_source": _finalize(by_source),
        "by_model": _finalize(by_model),
    }
