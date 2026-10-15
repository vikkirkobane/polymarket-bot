"""backend/main.py — FastAPI Command Center server."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.config import get_settings
from backend.database import init_db, get_db, SessionLocal, Trade, MarketOpportunity, CopyTrader, CopySignal, JournalEntry, Position
from backend.polymarket.client import PolymarketClient
from backend.polymarket.consensus import ConsensusEngine
from backend.polymarket.copy_trader import CopyTradingEngine
from backend.polymarket.researcher import MarketResearcher
from backend.analyst import get_analyst
from backend.trader.executor import TradeExecutor
from backend.trader.portfolio import Portfolio
from backend.trader.risk import RiskManager
from backend import analytics
from backend.assistant import AssistantService, build_platform_context

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("polybot")

# ── Global State (initialized in lifespan, not at import time) ─────────────────

settings = None
_poly_client = None
_analyst = None
_executor = None
_portfolio = None
_copy_engine = None
_researcher = None
_risk = None
_assistant: Optional["AssistantService"] = None
_consensus_engine: Optional["ConsensusEngine"] = None

bot_state: dict = {}


def quality_momentum_bonus(momentum_24h, side) -> float:
    """±10 quality points for tape corroboration (price moving toward our side)."""
    if momentum_24h is None:
        return 0.0
    s = (side or "YES").upper()
    aligned = (s == "YES" and momentum_24h > 0.01) or (s == "NO" and momentum_24h < -0.01)
    opposed = (s == "YES" and momentum_24h < -0.01) or (s == "NO" and momentum_24h > 0.01)
    return 10.0 if aligned else -10.0 if opposed else 0.0

# ── Paths ──────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

# ── WebSocket Manager ──────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)

ws_manager = ConnectionManager()

# ── Background Tasks ───────────────────────────────────────────────────────────

async def select_and_process_opportunities(opportunities: list[dict]):
    """Tiered trade selection logic."""
    if not opportunities:
        return

    # Sort opportunities by edge and confidence
    # Tier 1: Confidence High, Edge > 10%
    # Tier 2: Confidence Medium/High, Edge > 5%
    # Tier 3: Others
    
    tier1 = [o for o in opportunities if o.get("confidence") == "high" and o.get("edge_pct", 0) >= 10.0]
    tier2 = [o for o in opportunities if o.get("confidence") in ("medium", "high") and o.get("edge_pct", 0) >= 5.0]
    
    selected = None
    if tier1:
        selected = tier1[0]
        tier = "Tier 1 (High Confidence + High Edge)"
    elif tier2:
        selected = tier2[0]
        tier = "Tier 2 (Medium Confidence + Edge)"
    
    if not selected:
        return

    logger.info(f"Selected trade candidate: {selected['question']} ({tier})")
    
    if settings.auto_execute:
        resolved_side = selected.get("recommended_side") or selected.get("side") or "YES"
        trade = await _executor.execute(
            market_id=selected["market_id"],
            question=selected["question"],
            side=resolved_side,
            size_usd=settings.max_bet_size,
            price=selected.get("yes_price", 0.5) if resolved_side == "YES" else selected.get("no_price", 0.5),
            source="research",
            model_used=settings.ai_model,
            token_id=(selected.get("yes_token_id") if resolved_side == "YES" else selected.get("no_token_id")) or "",
        )
        await ws_manager.broadcast({"event": "trade_executed", "trade": trade})
    else:
        # Persist the flag in the DB so it survives page reloads
        db = SessionLocal()
        try:
            opp_record = (
                db.query(MarketOpportunity)
                .filter(MarketOpportunity.market_id == selected["market_id"])
                .order_by(MarketOpportunity.scanned_at.desc())
                .first()
            )
            if opp_record:
                opp_record.flagged_for_approval = True
                opp_record.scanned_at = datetime.utcnow()  # reset timer so 30-min window starts now
                db.commit()
                logger.info(f"Flagged opportunity for approval (id={opp_record.id}): {opp_record.question[:60]}")
        except Exception as exc:
            logger.error(f"Failed to flag opportunity for approval: {exc}")
        finally:
            db.close()

        # Alert the user via WebSocket
        alert_data = {
            "event": "trade_alert",
            "tier": tier,
            "opportunity": selected,
            "message": f"ALERT: Potential {tier} trade found! Click Approve to execute."
        }
        await ws_manager.broadcast(alert_data)
        logger.info(f"User alerted for pending trade: {selected['question']}")


async def run_scan_cycle():
    """Research scan background task."""
    logger.info("Running market research scan...")
    try:
        opportunities = await _researcher.run_scan()
        bot_state["last_scan"] = datetime.utcnow().isoformat()
        bot_state["scan_count"] += 1
        await ws_manager.broadcast({
            "event": "scan_complete",
            "opportunities": len(opportunities),
            "timestamp": bot_state["last_scan"],
        })
        
        if bot_state["running"]:
            await select_and_process_opportunities(opportunities)
            
    except Exception as exc:
        logger.error(f"Scan cycle error: {exc}")
        await ws_manager.broadcast({"event": "error", "message": str(exc)})


async def run_copy_cycle():
    """Copy trading poll background task."""
    if not settings.copy_trading_enabled:
        return
    logger.info("Polling copy traders...")
    try:
        signals = await _copy_engine.poll_and_generate_signals()
        if signals:
            bot_state["copy_signals_today"] += len(signals)
            bot_state["last_copy_poll"] = datetime.utcnow().isoformat()
            await ws_manager.broadcast({"event": "copy_signals", "count": len(signals), "signals": signals})
            
            if bot_state["running"]:
                for sig in signals:
                    if settings.auto_execute:
                        trade = await _executor.execute(
                            market_id=sig["market_id"],
                            question=sig["question"],
                            side=sig["side"],
                            size_usd=sig["copied_size"],
                            price=sig["price"],
                            source="copy",
                            token_id=sig.get("token_id", ""),
                        )
                        if trade.get("status") == "filled" and sig.get("_signal_id"):
                            _copy_engine.mark_signal_executed(sig["_signal_id"])
                        await ws_manager.broadcast({"event": "trade_executed", "trade": trade})
                    else:
                        # Alert for copy signal
                        await ws_manager.broadcast({
                            "event": "trade_alert",
                            "tier": "Copy Trade",
                            "signal": sig,
                            "message": f"ALERT: Copy trade signal from {sig['trader_name']}! Approve to execute."
                        })
    except Exception as exc:
        logger.error(f"Copy cycle error: {exc}")


def _purge_old_opportunities():
    """Delete unreviewed MarketOpportunity rows older than 7 days. Reviewed trades are never purged."""
    from datetime import timedelta
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=7)
        deleted = (
            db.query(MarketOpportunity)
            .filter(MarketOpportunity.scanned_at < cutoff)
            .filter(MarketOpportunity.outcome.is_(None))  # keep reviewed trades forever
            .delete(synchronize_session=False)
        )
        db.commit()
        if deleted:
            logger.info(f"[startup purge] Removed {deleted} unreviewed opportunities older than 7 days")
    except Exception as exc:
        logger.error(f"[startup purge] Failed: {exc}")
    finally:
        db.close()


async def _auto_scan_if_stale():
    """Trigger a scan on startup if no recent data exists (within 6 hours)."""
    from datetime import timedelta
    await asyncio.sleep(3)  # brief delay so server is fully ready
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=6)
        recent = db.query(MarketOpportunity).filter(MarketOpportunity.scanned_at >= cutoff).count()
        if recent == 0:
            logger.info("No recent scan data found — triggering auto-scan on startup")
            await run_scan_cycle()
        else:
            logger.info(f"Startup check: {recent} recent opportunities found, skipping auto-scan")
    except Exception as exc:
        logger.error(f"Auto-scan check failed: {exc}")
    finally:
        db.close()


async def bot_loop():
    """Main bot loop — runs scan and copy cycles on schedule."""
    scan_interval = settings.scan_interval_minutes * 60
    copy_interval = settings.copy_poll_interval_minutes * 60
    last_scan = 0.0
    last_copy = 0.0

    while bot_state["running"]:
        now = asyncio.get_event_loop().time()
        if now - last_scan >= scan_interval:
            await run_scan_cycle()
            last_scan = now
        if now - last_copy >= copy_interval:
            await run_copy_cycle()
            last_copy = now
        await asyncio.sleep(10)

_bot_task: Optional[asyncio.Task] = None

# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global settings, _poly_client, _analyst, _executor, _portfolio, _copy_engine, _researcher, _assistant, _consensus_engine, _risk, bot_state

    settings = get_settings()
    _poly_client = PolymarketClient(
        api_key=settings.poly_api_key,
        api_secret=settings.poly_api_secret,
        passphrase=settings.poly_passphrase,
        private_key=settings.poly_private_key,
        wallet_address_override=settings.poly_wallet_address,
    )
    _analyst = get_analyst(settings.ai_model, settings)
    global _risk
    _risk = RiskManager(
        kelly_fraction=settings.kelly_fraction,
        max_position_pct=settings.max_position_pct,
        max_open_positions=settings.max_open_positions,
        max_event_exposure_pct=settings.max_event_exposure_pct,
        max_total_exposure_pct=settings.max_total_exposure_pct,
        weekly_drawdown_limit_pct=settings.weekly_drawdown_limit_pct,
        max_spread_edge_ratio=settings.max_spread_edge_ratio,
        use_kelly_sizing=settings.use_kelly_sizing,
    )
    _executor = TradeExecutor(
        paper_mode=settings.paper_mode,
        client=_poly_client,  # always wired so paper→live switching can place real orders
        paper_balance=settings.paper_balance,
        max_daily_spend=settings.max_daily_spend,
        risk_manager=_risk,
    )
    _portfolio = Portfolio()
    _copy_engine = CopyTradingEngine(
        client=_poly_client,
        scale_factor=settings.copy_scale_factor,
        max_bet=settings.max_bet_size,
    )
    _researcher = MarketResearcher(
        client=_poly_client,
        analyst=_analyst,
        min_edge=settings.min_edge_percent,
        min_volume=settings.min_volume,
        limit=50,
    )
    _consensus_engine = ConsensusEngine(client=_poly_client)
    bot_state.update({
        "running": False,
        "mode": "paper" if settings.paper_mode else "live",
        "ai_model": settings.ai_model,
        "last_scan": None,
        "last_copy_poll": None,
        "scan_count": 0,
        "copy_signals_today": 0,
    })

    if not FRONTEND.exists():
        logger.warning(f"Frontend directory not found at {FRONTEND}")
    else:
        logger.info("Frontend directory found.")

    if settings.openrouter_api_key:
        _assistant = AssistantService(
            api_key=settings.openrouter_api_key,
            preferred_model=settings.openrouter_model,
        )
        logger.info(f"AI assistant initialized via OpenRouter (preferred={settings.openrouter_model})")
    else:
        logger.warning("OPENROUTER_API_KEY not set — AI assistant disabled")

    init_db()
    _purge_old_opportunities()
    logger.info(f"Polymarket Bot started | mode={bot_state['mode']} | model={settings.ai_model}")

    # Auto-scan if DB has no recent data so pages are populated on first load
    asyncio.create_task(_auto_scan_if_stale())
    yield
    global _bot_task
    if _bot_task:
        _bot_task.cancel()
    await _poly_client.aclose()
    logger.info("Polymarket client closed")

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Polymarket Bot Command Center", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
else:
    logger.error(f"Cannot mount /static: {FRONTEND} does not exist")


@app.get("/")
async def index():
    idx = FRONTEND / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return {"status": "ok", "message": "Polymarket Bot API running"}

# ── REST API ───────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return {
        **bot_state,
        "paper_balance": round(_executor.paper_balance, 2),
        "connections": len(ws_manager.active),
    }


class FundRequest(BaseModel):
    amount: float

@app.post("/api/bot/fund")
async def fund_paper_account(req: FundRequest):
    if req.amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    if req.amount > 1000000:  # Sanity cap
        raise HTTPException(400, "Amount too large (max $1M)")
    _executor._paper_balance += req.amount
    return {"status": "funded", "new_balance": round(_executor.paper_balance, 2)}


@app.post("/api/bot/start")
async def start_bot():
    global _bot_task
    if bot_state["running"]:
        return {"status": "already_running"}
    bot_state["running"] = True
    _bot_task = asyncio.create_task(bot_loop())
    await ws_manager.broadcast({"event": "bot_started", "mode": bot_state["mode"]})
    return {"status": "started"}


@app.post("/api/bot/stop")
async def stop_bot():
    global _bot_task
    bot_state["running"] = False
    if _bot_task:
        _bot_task.cancel()
        _bot_task = None
    await ws_manager.broadcast({"event": "bot_stopped"})
    return {"status": "stopped"}


class ModeRequest(BaseModel):
    mode: str  # paper | live

@app.post("/api/bot/mode")
async def set_mode(req: ModeRequest):
    if req.mode not in ("paper", "live"):
        raise HTTPException(400, "mode must be 'paper' or 'live'")
    if req.mode == "live":
        # Validate real-money prerequisites BEFORE switching
        if not settings.poly_private_key:
            raise HTTPException(400, "Cannot switch to live: POLY_PRIVATE_KEY is not set in .env")
        try:
            import polymarket  # noqa: F401
        except ImportError:
            raise HTTPException(400, "Cannot switch to live: official SDK missing — pip install polymarket-client")
        if not _poly_client.wallet_address:
            raise HTTPException(400, "Cannot switch to live: wallet address could not be derived/set (POLY_PRIVATE_KEY or POLY_WALLET_ADDRESS)")
        _executor.client = _poly_client
    bot_state["mode"] = req.mode
    _executor.paper_mode = req.mode == "paper"
    await ws_manager.broadcast({"event": "mode_changed", "mode": req.mode})
    return {"mode": req.mode}


class ModelRequest(BaseModel):
    model: str  # claude | openai | gemini | mock

@app.post("/api/bot/model")
async def set_model(req: ModelRequest):
    global _analyst, _researcher
    try:
        _analyst = get_analyst(req.model, settings)
        _researcher.analyst = _analyst
        bot_state["ai_model"] = req.model
        return {"model": req.model}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/portfolio")
async def portfolio():
    summary = _portfolio.get_summary(paper_balance=_executor.paper_balance)
    daily = _portfolio.get_daily_pnl(days=30)
    return {**summary, "daily_pnl": daily}


@app.get("/api/positions")
async def positions():
    return _portfolio.get_positions()


@app.get("/api/trades")
async def trades(limit: int = 100, source: str = None):
    return _portfolio.get_trades(limit=limit, source=source)


# ── My Profile ─────────────────────────────────────────────────────────────────

@app.get("/api/me/profile")
async def my_profile():
    """Your Polymarket profile stats (wallet address derived from POLY_PRIVATE_KEY)."""
    profile = await _poly_client.get_my_profile()
    if not profile:
        raise HTTPException(404, "Wallet address could not be derived — ensure POLY_PRIVATE_KEY is set in .env")
    return profile


@app.get("/api/me/trades")
async def my_trades(limit: int = 50):
    """Your Polymarket on-chain trade history."""
    return await _poly_client.get_my_trade_history(limit=limit)


@app.get("/api/me/positions")
async def my_positions():
    """Your current open positions on Polymarket."""
    return await _poly_client.get_my_open_positions()


@app.get("/api/me/pnl-chart")
async def my_pnl_chart(days: int = 30):
    """
    Build a daily net-cash-flow series from on-chain trade history.
    Daily value = sum(SELL sizes) - sum(BUY sizes) for that day.
    Cumulative running total approximates realized P&L over time.
    """
    from collections import defaultdict
    from datetime import timedelta

    trades = await _poly_client.get_my_trade_history(limit=200)
    if not trades:
        return {"data": [], "source": "polymarket"}

    cutoff = datetime.utcnow() - timedelta(days=days)
    daily: dict[str, float] = defaultdict(float)

    for t in trades:
        ts = t.get("timestamp", "")
        trade_dt = None
        try:
            if isinstance(ts, (int, float)) and ts > 0:
                trade_dt = datetime.utcfromtimestamp(float(ts))
            elif isinstance(ts, str) and ts:
                trade_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass

        if trade_dt is None or trade_dt < cutoff:
            continue

        size = t.get("size", 0) or 0
        action = t.get("action", "").upper()
        # SELL = cash inflow (+), BUY = cash outflow (-)
        flow = size if action == "SELL" else -size
        day_key = trade_dt.strftime("%Y-%m-%d")
        daily[day_key] += flow

    if not daily:
        return {"data": [], "source": "polymarket"}

    # Sort chronologically and format labels
    sorted_days = sorted(daily.keys())
    result = []
    for day in sorted_days:
        dt = datetime.strptime(day, "%Y-%m-%d")
        result.append({
            "date": dt.strftime("%b %d"),
            "pnl": round(daily[day], 2),
        })

    return {"data": result, "source": "polymarket"}


# ── Copy Trading ───────────────────────────────────────────────────────────────

@app.get("/api/copy-traders")
async def get_copy_traders():
    return _copy_engine.get_all_traders()


class AddTraderRequest(BaseModel):
    address: str
    name: Optional[str] = None

@app.post("/api/copy-traders")
async def add_copy_trader(req: AddTraderRequest):
    """Add a trader and fetch their stats from API."""
    trader = await _copy_engine.add_trader_with_stats(req.address, req.name)
    if trader:
        return {"status": "added", "id": trader.id, "name": trader.name, "roi": trader.roi, "win_rate": trader.win_rate}
    return {"status": "error", "message": "Failed to add trader"}


class FollowTraderRequest(BaseModel):
    address: str

@app.post("/api/copy-traders/follow")
async def follow_copy_trader(req: FollowTraderRequest):
    """Follow a trader from leaderboard - fetches full stats."""
    trader = await _copy_engine.follow_trader(req.address)
    if trader:
        return {
            "status": "followed",
            "id": trader.id,
            "name": trader.name,
            "roi": trader.roi,
            "win_rate": trader.win_rate,
            "total_profit": trader.total_profit,
        }
    return {"status": "error", "message": "Failed to follow trader"}


@app.delete("/api/copy-traders/{trader_id}")
async def remove_copy_trader(trader_id: int):
    ok = _copy_engine.remove_trader(trader_id)
    if not ok:
        raise HTTPException(404, "Trader not found")
    return {"status": "removed"}


@app.get("/api/copy-signals")
async def copy_signals(limit: int = 50):
    return _copy_engine.get_recent_signals(limit=limit)


@app.get("/api/copy-traders/leaderboard")
async def get_leaderboard(limit: int = 20):
    """Fetch top traders from Polymarket API for display with Follow buttons."""
    try:
        traders = await _poly_client.get_top_traders(limit=limit)
        # Get currently followed addresses
        followed = {t["address"] for t in _copy_engine.get_all_traders() if t.get("active")}
        
        return [
            {
                "address": t["address"],
                "name": t["name"],
                "roi": t["roi"],
                "profit": t["profit"],
                "volume": t["volume"],
                "win_rate": t["win_rate"],
                "positions_count": t["positions_count"],
                "is_followed": t["address"] in followed,
            }
            for t in traders
        ]
    except Exception as exc:
        logger.error(f"Failed to fetch leaderboard: {exc}")
        return []


@app.post("/api/copy-traders/refresh")
async def refresh_copy_traders(background_tasks: BackgroundTasks):
    background_tasks.add_task(_copy_engine.refresh_top_traders, 20)
    return {"status": "refresh_queued"}


# ── Market Research ────────────────────────────────────────────────────────────

@app.get("/api/opportunities")
async def opportunities():
    from datetime import datetime
    all_opps = _researcher.get_latest_opportunities(limit=200)
    result = []
    for opp in all_opps:
        end_date_str = opp.get("end_date", "")
        days_until = None
        try:
            if end_date_str:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_until = (end_date - datetime.now(end_date.tzinfo)).days
        except Exception:
            pass
        if days_until is not None and days_until < 0:
            continue  # drop expired
        opp["days_until"] = days_until
        result.append(opp)
    return result


@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_scan_cycle)
    return {"status": "scan_queued"}


@app.get("/api/pending-approvals")
async def pending_approvals(limit: int = 500):
    """
    High-conviction, near-term picks:
    - HIGH confidence only (not medium — those go to Opportunities for review)
    - Edge >= 5%
    - Event within 30 days OR no event date set (near-term actionable)
    - Events > 30 days away are intentionally excluded (they belong on Future Trades)
    - Sorted by quality score DESC, then event date ASC
    """
    from datetime import datetime, timezone

    now_utc = datetime.now(timezone.utc)

    # Get latest opportunities from the last scan
    opportunities = _researcher.get_latest_opportunities(limit=200)
    
    # Get already traded market IDs to exclude
    db = SessionLocal()
    try:
        traded_markets = {t.market_id for t in db.query(Trade.market_id).filter(
            Trade.status.in_(["filled", "pending"]),
            Trade.source == "research"
        ).all()}
    finally:
        db.close()
    
    # Filter out already traded markets and calculate quality score
    pending = []
    for opp in opportunities:
        if opp.get("market_id") in traded_markets:
            continue
        
        # Quality scoring
        edge = opp.get("edge_pct", 0)
        confidence = opp.get("confidence", "low")
        volume = opp.get("volume_24h", 0)
        trader_matches = len(opp.get("trader_matches", []))
        
        # Best Picks = HIGH + MEDIUM confidence with meaningful edge
        # HIGH confidence picks get better quality scores (20 pts vs 10 pts) so they naturally sort first
        if edge < 3.0 or confidence not in ("high", "medium"):
            continue
        
        # Calculate quality score (0-100)
        # Edge: up to 40 points (capped at 20% edge)
        edge_score = min(edge * 2, 40)
        
        # Confidence: 0/10/20 points
        conf_score = {"low": 0, "medium": 10, "high": 20}.get(confidence, 0)
        
        # Volume: up to 20 points (log scale, $1M+ = full points)
        vol_score = min(20, (volume / 1000000) * 20) if volume > 0 else 0
        
        # Trader alignment: up to 20 points (2+ traders = full)
        trader_score = min(trader_matches * 10, 20)
        
        quality_score = edge_score + conf_score + vol_score + trader_score
        
        # Parse event date for sorting and expiry check
        end_date_str = opp.get("end_date", "")
        event_date = None
        try:
            if end_date_str:
                event_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                if event_date.tzinfo is None:
                    event_date = event_date.replace(tzinfo=timezone.utc)
        except Exception:
            pass

        # Skip expired events
        if event_date is not None and event_date < now_utc:
            continue

        seconds_until = (event_date - now_utc).total_seconds() if event_date else None
        days_until = int(seconds_until // 86400) if seconds_until is not None else None

        # Skip far-future events — those belong on Future Trades (> 45 days away)
        # Markets with no end_date are kept (they're near-term by default)
        if seconds_until is not None and seconds_until > 45 * 86400:
            continue

        pending.append({
            **opp,
            "quality_score": round(quality_score, 1),
            "tier": "🥇 GOLD" if quality_score >= 80 else "🥈 SILVER" if quality_score >= 60 else "🥉 BRONZE",
            "event_date": event_date.isoformat() if event_date else None,
            "event_date_display": event_date.strftime("%b %d, %Y") if event_date else "Unknown",
            "days_until": days_until,
        })
    
    # Sort by: quality score (best first), then event date ascending within same tier
    pending.sort(key=lambda x: (-x["quality_score"], x.get("event_date") or "9999"))
    
    return {
        "total_pending": len(pending),
        "showing": len(pending),
        "approvals": pending,
    }


# ── Cleanup / Maintenance ───────────────────────────────────────────────────────

@app.post("/api/cleanup/expired")
async def cleanup_expired_opportunities():
    """Remove opportunities where the event has already ended and old scanned data."""
    from datetime import datetime, timedelta

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        seven_days_ago = now - timedelta(days=7)

        # Delete stale scans directly via SQL (scanned_at is a proper DateTime column)
        stale_deleted = (
            db.query(MarketOpportunity)
            .filter(MarketOpportunity.scanned_at < seven_days_ago)
            .delete(synchronize_session=False)
        )

        # For expired events, end_date is stored as string so we filter in Python —
        # but only load non-stale rows that have an end_date set.
        candidates = (
            db.query(MarketOpportunity)
            .filter(
                MarketOpportunity.scanned_at >= seven_days_ago,
                MarketOpportunity.end_date.isnot(None),
                MarketOpportunity.end_date != "",
            )
            .all()
        )

        expired_count = 0
        for opp in candidates:
            event_date = None
            try:
                event_date = datetime.fromisoformat(opp.end_date.replace("Z", "+00:00"))
            except Exception:
                try:
                    event_date = datetime.strptime(opp.end_date, "%Y-%m-%d")
                except Exception:
                    pass

            if event_date and event_date.replace(tzinfo=None) < now:
                logger.info(f"Removing expired: {opp.question[:50]}… (ended {event_date.date()})")
                db.delete(opp)
                expired_count += 1

        db.commit()
        total_removed = stale_deleted + expired_count
        logger.info(f"Cleanup: removed {total_removed} ({expired_count} expired, {stale_deleted} stale)")
        return {
            "status": "success",
            "removed": total_removed,
            "expired_events": expired_count,
            "stale_scans": stale_deleted,
            "timestamp": now.isoformat(),
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Cleanup failed: {e}")
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")
    finally:
        db.close()


@app.get("/api/cleanup/stats")
async def get_cleanup_stats():
    """Get statistics about expired/old opportunities without deleting them."""
    from datetime import datetime, timedelta

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        seven_days_ago = now - timedelta(days=7)

        total = db.query(MarketOpportunity).count()
        stale = (
            db.query(MarketOpportunity)
            .filter(MarketOpportunity.scanned_at < seven_days_ago)
            .count()
        )

        # Python loop only over non-stale rows with end_dates
        candidates = (
            db.query(MarketOpportunity)
            .filter(
                MarketOpportunity.scanned_at >= seven_days_ago,
                MarketOpportunity.end_date.isnot(None),
                MarketOpportunity.end_date != "",
            )
            .all()
        )

        expired = 0
        active = 0
        upcoming = []

        for opp in candidates:
            event_date = None
            try:
                event_date = datetime.fromisoformat(opp.end_date.replace("Z", "+00:00"))
            except Exception:
                try:
                    event_date = datetime.strptime(opp.end_date, "%Y-%m-%d")
                except Exception:
                    pass

            if event_date:
                days_until = (event_date.replace(tzinfo=None) - now).days
                if days_until < 0:
                    expired += 1
                else:
                    active += 1
                    if days_until <= 7:
                        upcoming.append({
                            "question": opp.question[:60],
                            "days_until": days_until,
                            "market_id": opp.market_id,
                        })
            else:
                active += 1

        return {
            "total_opportunities": total,
            "expired_events": expired,
            "stale_scans": stale,
            "active_recent": active,
            "closing_soon": sorted(upcoming, key=lambda x: x["days_until"])[:5],
            "can_cleanup": expired + stale > 0,
        }

    finally:
        db.close()


# ── Manual Trade ───────────────────────────────────────────────────────────────

class ManualTradeRequest(BaseModel):
    market_id: str
    question: str
    side: str
    size_usd: float
    price: float
    token_id: str = ""  # optional CLOB token id (required for live; resolved if missing)

@app.post("/api/trade")
async def manual_trade(req: ManualTradeRequest):
    trade = await _executor.execute(
        market_id=req.market_id,
        question=req.question,
        side=req.side,
        size_usd=req.size_usd,
        price=req.price,
        source="manual",
        token_id=req.token_id,
    )
    await ws_manager.broadcast({"event": "trade_executed", "trade": trade})
    return trade


# ── Dashboard Pending Trades (persisted) ───────────────────────────────────────

@app.get("/api/pending-trades/dashboard")
async def get_dashboard_pending_trades():
    """Returns pending approvals flagged within the last 30 minutes.
    Trades older than 30 minutes are auto-unflagged so the list stays current."""
    from datetime import timedelta
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=30)

        # Auto-expire research opportunities older than 30 minutes
        expired = (
            db.query(MarketOpportunity)
            .filter(
                MarketOpportunity.flagged_for_approval == True,
                MarketOpportunity.scanned_at < cutoff,
            )
            .all()
        )
        for e in expired:
            e.flagged_for_approval = False
        if expired:
            db.commit()
            logger.info(f"Auto-expired {len(expired)} pending approval(s) older than 30 minutes")

        # Markets already executed — exclude from research pending
        traded_markets = {
            t.market_id for t in db.query(Trade.market_id).filter(
                Trade.status.in_(["filled", "pending"])
            ).all()
        }

        research = (
            db.query(MarketOpportunity)
            .filter(
                MarketOpportunity.flagged_for_approval == True,
                MarketOpportunity.market_id.notin_(traded_markets),
                MarketOpportunity.scanned_at >= cutoff,
            )
            .order_by(MarketOpportunity.scanned_at.desc())
            .all()
        )

        copy_sigs = (
            db.query(CopySignal)
            .filter(
                CopySignal.status == "pending",
                CopySignal.timestamp >= cutoff,
            )
            .order_by(CopySignal.timestamp.desc())
            .all()
        )

        results = []
        for opp in research:
            tier = (
                "Tier 1 (High Confidence + High Edge)"
                if opp.confidence == "high" and (opp.edge_pct or 0) >= 10
                else "Tier 2 (Medium Confidence + Edge)"
            )
            results.append({
                "tier": tier,
                "opportunity": {
                    "id": opp.id,
                    "market_id": opp.market_id,
                    "question": opp.question,
                    "side": opp.side,
                    "yes_price": opp.yes_price,
                    "no_price": opp.no_price,
                    "edge_pct": opp.edge_pct,
                    "confidence": opp.confidence,
                    "model_used": opp.model_used,
                    "analysis": opp.analysis,
                    "yes_token_id": opp.yes_token_id or "",
                    "no_token_id": opp.no_token_id or "",
                    "outcome": opp.outcome,
                    "outcome_marked_at": opp.outcome_marked_at.isoformat() if opp.outcome_marked_at else None,
                },
            })

        for sig in copy_sigs:
            results.append({
                "tier": "Copy Trade",
                "signal": {
                    "_signal_id": sig.id,
                    "market_id": sig.market_id,
                    "question": sig.question,
                    "side": sig.side,
                    "trader_address": sig.trader_address,
                    "trader_name": sig.trader_name,
                    "original_size": sig.original_size,
                    "copied_size": sig.copied_size,
                    "price": sig.price,
                    "outcome": sig.outcome,
                },
            })

        return {"pending": results, "total": len(results)}
    finally:
        db.close()


@app.post("/api/pending-trades/research/{opp_id}/dismiss")
async def dismiss_research_pending(opp_id: int):
    db = SessionLocal()
    try:
        opp = db.query(MarketOpportunity).filter(MarketOpportunity.id == opp_id).first()
        if opp:
            opp.flagged_for_approval = False
            db.commit()
            logger.info(f"Dismissed research pending approval id={opp_id}")
        return {"status": "dismissed"}
    finally:
        db.close()


@app.post("/api/pending-trades/signal/{signal_id}/dismiss")
async def dismiss_signal_pending(signal_id: int):
    db = SessionLocal()
    try:
        sig = db.query(CopySignal).filter(CopySignal.id == signal_id).first()
        if sig:
            sig.status = "skipped"
            db.commit()
            logger.info(f"Dismissed copy signal id={signal_id} (trader: {sig.trader_name})")
        return {"status": "dismissed"}
    finally:
        db.close()


# ── Outcome Marking ────────────────────────────────────────────────────────────

class OutcomeRequest(BaseModel):
    outcome: str  # 'success' | 'failure'

@app.post("/api/opportunities/{opp_id}/outcome")
async def mark_opportunity_outcome(opp_id: int, req: OutcomeRequest):
    if req.outcome not in ("success", "failure"):
        raise HTTPException(400, "outcome must be 'success' or 'failure'")
    db = SessionLocal()
    try:
        opp = db.query(MarketOpportunity).filter(MarketOpportunity.id == opp_id).first()
        if not opp:
            raise HTTPException(404, "Opportunity not found")
        opp.outcome = req.outcome
        opp.outcome_marked_at = datetime.utcnow()
        db.commit()
        logger.info(f"Outcome '{req.outcome}' saved for opportunity {opp_id}: {opp.question[:60]}")
        return {"status": "updated", "id": opp_id, "outcome": req.outcome}
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.error(f"Failed to save outcome for opportunity {opp_id}: {exc}")
        raise HTTPException(500, "Failed to save outcome to database")
    finally:
        db.close()


class MarketOutcomeRequest(BaseModel):
    market_id: str
    outcome: str        # 'success' | 'failure'
    question: str = "" # used when creating a stub record (e.g. consensus cards)

@app.post("/api/outcomes/by-market")
async def mark_outcome_by_market(req: MarketOutcomeRequest):
    """Mark the latest opportunity for a market_id as success/failure.
    If no opportunity exists (e.g. consensus cards), upserts a minimal stub record."""
    if req.outcome not in ("success", "failure"):
        raise HTTPException(400, "outcome must be 'success' or 'failure'")
    db = SessionLocal()
    try:
        opp = (
            db.query(MarketOpportunity)
            .filter(MarketOpportunity.market_id == req.market_id)
            .order_by(MarketOpportunity.scanned_at.desc())
            .first()
        )
        if not opp:
            opp = MarketOpportunity(
                market_id=req.market_id,
                question=req.question or req.market_id,
                yes_price=0.5,
                no_price=0.5,
                side="YES",
                confidence="low",
                model_used="consensus",
                analysis="",
            )
            db.add(opp)
        opp.outcome = req.outcome
        opp.outcome_marked_at = datetime.utcnow()
        db.commit()
        db.refresh(opp)
        logger.info(f"Outcome '{req.outcome}' saved for market {req.market_id}: {(opp.question or '')[:60]}")
        return {"status": "updated", "id": opp.id, "market_id": req.market_id, "outcome": req.outcome}
    except Exception as exc:
        db.rollback()
        logger.error(f"Failed to save outcome for market {req.market_id}: {exc}")
        raise HTTPException(500, "Failed to save outcome to database")
    finally:
        db.close()


@app.post("/api/copy-signals/{signal_id}/outcome")
async def mark_signal_outcome(signal_id: int, req: OutcomeRequest):
    """Mark a copy trade signal as success/failure for trader performance ranking."""
    if req.outcome not in ("success", "failure"):
        raise HTTPException(400, "outcome must be 'success' or 'failure'")
    db = SessionLocal()
    try:
        sig = db.query(CopySignal).filter(CopySignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404, "Copy signal not found")
        sig.outcome = req.outcome
        sig.outcome_marked_at = datetime.utcnow()
        db.commit()
        logger.info(f"Signal outcome '{req.outcome}' saved — trader: {sig.trader_name}, market: {sig.question[:50]}")
        return {"status": "updated", "id": signal_id, "trader": sig.trader_name, "outcome": req.outcome}
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.error(f"Failed to save signal outcome {signal_id}: {exc}")
        raise HTTPException(500, "Failed to save outcome to database")
    finally:
        db.close()


@app.get("/api/reviewed-opportunities")
async def reviewed_opportunities(limit: int = 50):
    """Return all MarketOpportunity rows that have been outcome-marked, newest first."""
    db = SessionLocal()
    try:
        rows = (
            db.query(MarketOpportunity)
            .filter(MarketOpportunity.outcome.isnot(None))
            .order_by(MarketOpportunity.outcome_marked_at.desc())
            .limit(limit)
            .all()
        )
        result = []
        for r in rows:
            trader_matches = []
            try:
                trader_matches = json.loads(r.trader_matches or "[]")
            except Exception:
                pass
            result.append({
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
                "trader_matches": trader_matches,
                "end_date": r.end_date,
                "scanned_at": r.scanned_at.isoformat() if r.scanned_at else None,
                "outcome": r.outcome,
                "outcome_marked_at": r.outcome_marked_at.isoformat() if r.outcome_marked_at else None,
            })
        return {"reviewed": result, "total": len(result)}
    finally:
        db.close()


@app.get("/api/future-trades")
async def future_trades():
    """
    Long-horizon opportunities: event strictly more than 30 days away.
    Requires a set end_date. Any confidence level welcome (scanner shows ALL,
    Best Picks handles near-term high-confidence). Excludes reviewed and traded markets.
    """
    from datetime import timedelta, timezone

    db = SessionLocal()
    try:
        cutoff_7d = datetime.utcnow() - timedelta(days=7)

        # Broader scan window (7 days) since far-future markets appear less often
        rows = (
            db.query(MarketOpportunity)
            .filter(
                MarketOpportunity.outcome.is_(None),
                MarketOpportunity.edge_pct >= 3.0,
                MarketOpportunity.scanned_at >= cutoff_7d,
                MarketOpportunity.end_date.isnot(None),
            )
            .order_by(MarketOpportunity.scanned_at.desc())
            .limit(500)
            .all()
        )

        traded_markets = {t.market_id for t in db.query(Trade.market_id).filter(
            Trade.status.in_(["filled", "pending"]),
            Trade.source == "research",
        ).all()}

        result = []
        now_utc = datetime.now(timezone.utc)

        for r in rows:
            if r.market_id in traded_markets:
                continue

            end_date_obj = None
            seconds_until = None
            try:
                if r.end_date:
                    end_date_obj = datetime.fromisoformat(r.end_date.replace("Z", "+00:00"))
                    if end_date_obj.tzinfo is None:
                        end_date_obj = end_date_obj.replace(tzinfo=timezone.utc)
                    seconds_until = (end_date_obj - now_utc).total_seconds()
            except Exception:
                pass

            # Drop events that have already ended
            if end_date_obj is not None and end_date_obj < now_utc:
                continue

            days_until = int(seconds_until // 86400) if seconds_until is not None else None

            # Future Trades = strictly more than 30 days out; must have a date
            if seconds_until is None or seconds_until <= 30 * 86400:
                continue

            try:
                trader_matches = json.loads(r.trader_matches or "[]")
            except Exception:
                trader_matches = []

            edge = r.edge_pct or 0
            volume = r.volume_24h or 0
            edge_score = min(edge * 2, 40)
            conf_score = {"low": 0, "medium": 10, "high": 20}.get(r.confidence or "low", 0)
            vol_score = min(20, (volume / 1_000_000) * 20) if volume > 0 else 0
            trader_score = min(len(trader_matches) * 10, 20)
            quality_score = round(edge_score + conf_score + vol_score + trader_score, 1)

            # Bucket by time horizon for frontend grouping
            if days_until is not None:
                if days_until <= 60:
                    horizon = "30–60 Days"
                elif days_until <= 90:
                    horizon = "2–3 Months"
                elif days_until <= 180:
                    horizon = "3–6 Months"
                else:
                    horizon = "6+ Months"
            else:
                horizon = "Unknown"

            result.append({
                "id": r.id,
                "market_id": r.market_id,
                "question": r.question,
                "yes_price": r.yes_price,
                "no_price": r.no_price,
                "volume_24h": volume,
                "edge_pct": edge,
                "side": r.side,
                "confidence": r.confidence,
                "model_used": r.model_used,
                "analysis": r.analysis,
                "trader_matches": trader_matches,
                "end_date": r.end_date,
                "event_date": end_date_obj.isoformat() if end_date_obj else None,
                "event_date_display": end_date_obj.strftime("%b %d, %Y") if end_date_obj else "Unknown",
                "days_until": days_until,
                "horizon": horizon,
                "quality_score": quality_score,
                "tier": "🥇 GOLD" if quality_score >= 80 else "🥈 SILVER" if quality_score >= 60 else "🥉 BRONZE",
                "outcome": r.outcome,
                "outcome_marked_at": r.outcome_marked_at.isoformat() if r.outcome_marked_at else None,
                "scanned_at": r.scanned_at.isoformat() if r.scanned_at else None,
            })

        # Sort by event date ascending (nearest long-term event first), then quality desc
        result.sort(key=lambda x: (x.get("event_date") or "9999", -x["quality_score"]))
        return {"trades": result, "total": len(result)}
    finally:
        db.close()


# ── Consensus Trades ───────────────────────────────────────────────────────────

@app.get("/api/consensus-trades")
async def get_consensus_trades(min_traders: int = 3, leaderboard_n: int = 20):
    """Markets where >= min_traders top traders share the same side.
    Merges followed traders (DB) with leaderboard top-N. Results are cached 5 min."""
    min_traders = max(2, min(min_traders, 20))
    leaderboard_n = max(5, min(leaderboard_n, 50))
    return await _consensus_engine.get_consensus_trades(
        min_traders=min_traders, leaderboard_n=leaderboard_n
    )


class ConsensusApproveRequest(BaseModel):
    side: str
    question: str
    size_usd: float = 50.0
    price: float = 0.5


@app.post("/api/consensus-trades/{market_id}/approve")
async def approve_consensus_trade(market_id: str, req: ConsensusApproveRequest):
    """Execute a consensus trade through the standard executor (paper or live)."""
    trade = await _executor.execute(
        market_id=market_id,
        question=req.question,
        side=req.side.upper(),
        size_usd=req.size_usd,
        price=req.price,
        source="consensus",
    )
    await ws_manager.broadcast({"event": "trade_executed", "trade": trade})
    return trade


class ConsensusDismissRequest(BaseModel):
    side: str


@app.post("/api/consensus-trades/{market_id}/dismiss")
async def dismiss_consensus_trade(market_id: str, req: ConsensusDismissRequest):
    """Suppress a consensus entry for the current server session."""
    _consensus_engine.dismiss(market_id, req.side.upper())
    return {"status": "dismissed"}


@app.post("/api/consensus-trades/refresh")
async def refresh_consensus_cache():
    """Force-clear the consensus position cache so the next GET fetches fresh data."""
    _consensus_engine.clear_cache()
    return {"status": "cache_cleared"}


# ── Rankings ───────────────────────────────────────────────────────────────────

@app.get("/api/rankings")
async def get_rankings(time_filter: str = "all"):
    from datetime import timedelta

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        cutoff = None
        if time_filter == "7d":
            cutoff = now - timedelta(days=7)
        elif time_filter == "30d":
            cutoff = now - timedelta(days=30)

        opp_q = db.query(MarketOpportunity).filter(MarketOpportunity.outcome.isnot(None))
        if cutoff:
            opp_q = opp_q.filter(MarketOpportunity.outcome_marked_at >= cutoff)
        marked_opps = opp_q.all()

        sig_rated_q = db.query(CopySignal).filter(CopySignal.outcome.isnot(None))
        if cutoff:
            sig_rated_q = sig_rated_q.filter(CopySignal.outcome_marked_at >= cutoff)
        marked_sigs = sig_rated_q.all()

        opp_success = sum(1 for m in marked_opps if m.outcome == "success")
        opp_failure = sum(1 for m in marked_opps if m.outcome == "failure")
        sig_success = sum(1 for s in marked_sigs if s.outcome == "success")
        sig_failure = sum(1 for s in marked_sigs if s.outcome == "failure")
        success_count = opp_success + sig_success
        failure_count = opp_failure + sig_failure
        total_marked = success_count + failure_count
        success_rate = round(success_count / total_marked * 100, 1) if total_marked else 0

        def compute_quality(edge_pct, confidence, volume_24h, trader_matches_json):
            edge = edge_pct or 0
            conf = confidence or "low"
            volume = volume_24h or 0
            try:
                trader_count = len(json.loads(trader_matches_json)) if trader_matches_json else 0
            except Exception:
                trader_count = 0
            edge_score = min(edge * 2, 40)
            conf_score = {"low": 0, "medium": 10, "high": 20}.get(conf, 0)
            vol_score = min(20, (volume / 1_000_000) * 20) if volume > 0 else 0
            trader_score = min(trader_count * 10, 20)
            return round(edge_score + conf_score + vol_score + trader_score, 1)

        def tier_label(score):
            return "GOLD" if score >= 80 else "SILVER" if score >= 60 else "BRONZE"

        # Top winning trades — AI research wins + copy signal wins, sorted by quality score
        winning_opps = [
            {
                "id": m.id,
                "source": "research",
                "question": m.question,
                "side": m.side,
                "edge_pct": m.edge_pct or 0,
                "confidence": m.confidence,
                "model_used": m.model_used,
                "trader_name": None,
                "outcome": m.outcome,
                "outcome_marked_at": m.outcome_marked_at.isoformat() if m.outcome_marked_at else None,
                "quality_score": compute_quality(m.edge_pct, m.confidence, m.volume_24h, m.trader_matches),
            }
            for m in marked_opps if m.outcome == "success"
        ]
        winning_sigs = [
            {
                "id": s.id,
                "source": "copy",
                "question": s.question,
                "side": s.side,
                "edge_pct": 0,
                "confidence": "medium",
                "model_used": None,
                "trader_name": s.trader_name,
                "outcome": s.outcome,
                "outcome_marked_at": s.outcome_marked_at.isoformat() if s.outcome_marked_at else None,
                "quality_score": compute_quality(0, "medium", 0, None),
            }
            for s in marked_sigs if s.outcome == "success"
        ]
        all_winners = sorted(winning_opps + winning_sigs, key=lambda x: x["quality_score"], reverse=True)
        top_trades = [
            {**t, "tier": tier_label(t["quality_score"])}
            for t in all_winners[:20]
        ]

        # AI model accuracy rankings (research opportunities only — copy signals have no model)
        model_stats: dict = {}
        for m in marked_opps:
            model = m.model_used or "unknown"
            if model not in model_stats:
                model_stats[model] = {"success": 0, "failure": 0, "edges": []}
            model_stats[model][m.outcome] += 1
            if m.edge_pct:
                model_stats[model]["edges"].append(m.edge_pct)

        ai_rankings = sorted(
            [
                {
                    "model": model,
                    "success_count": s["success"],
                    "failure_count": s["failure"],
                    "total_marked": s["success"] + s["failure"],
                    "accuracy": round(s["success"] / (s["success"] + s["failure"]) * 100, 1)
                    if (s["success"] + s["failure"]) else 0,
                    "avg_edge": round(sum(s["edges"]) / len(s["edges"]), 1) if s["edges"] else 0,
                }
                for model, s in model_stats.items()
            ],
            key=lambda x: x["accuracy"],
            reverse=True,
        )

        best_model = ai_rankings[0]["model"] if ai_rankings else None
        best_model_accuracy = ai_rankings[0]["accuracy"] if ai_rankings else 0

        # Trader leaderboard — combine Polymarket API stats with user-rated signal outcomes
        traders = (
            db.query(CopyTrader)
            .filter(CopyTrader.active == True)
            .order_by(CopyTrader.roi.desc())
            .limit(20)
            .all()
        )

        # Per-trader signal outcomes (user-rated copy trades)
        sig_q = db.query(CopySignal).filter(CopySignal.outcome.isnot(None))
        if cutoff:
            sig_q = sig_q.filter(CopySignal.outcome_marked_at >= cutoff)
        rated_signals = sig_q.all()

        trader_signal_stats: dict = {}
        for s in rated_signals:
            addr = s.trader_address
            if addr not in trader_signal_stats:
                trader_signal_stats[addr] = {"wins": 0, "losses": 0, "name": s.trader_name or "Unknown"}
            if s.outcome == "success":
                trader_signal_stats[addr]["wins"] += 1
            else:
                trader_signal_stats[addr]["losses"] += 1

        trader_rankings = []
        for t in traders:
            sig_stats = trader_signal_stats.get(t.address, {})
            wins = sig_stats.get("wins", 0)
            losses = sig_stats.get("losses", 0)
            rated_total = wins + losses
            user_accuracy = round(wins / rated_total * 100, 1) if rated_total else None
            trader_rankings.append({
                "name": t.name or "Unknown",
                "address": t.address,
                "roi": t.roi,
                "win_rate": t.win_rate,
                "total_profit": t.total_profit,
                "volume": t.volume,
                "user_wins": wins,
                "user_losses": losses,
                "user_rated": rated_total,
                "user_accuracy": user_accuracy,
            })

        # Also include traders who have rated signals but aren't in CopyTrader table
        followed_addresses = {t.address for t in traders}
        for addr, stats in trader_signal_stats.items():
            if addr not in followed_addresses:
                wins = stats["wins"]
                losses = stats["losses"]
                rated_total = wins + losses
                trader_rankings.append({
                    "name": stats["name"],
                    "address": addr,
                    "roi": None,
                    "win_rate": None,
                    "total_profit": None,
                    "volume": None,
                    "user_wins": wins,
                    "user_losses": losses,
                    "user_rated": rated_total,
                    "user_accuracy": round(wins / rated_total * 100, 1) if rated_total else None,
                })

        # Sort: traders with user ratings first (by user accuracy), then by ROI
        trader_rankings.sort(key=lambda x: (
            -(x["user_accuracy"] or -1),
            -(x["roi"] or 0),
        ))

        return {
            "stats": {
                "total_marked": total_marked,
                "success_count": success_count,
                "failure_count": failure_count,
                "success_rate": success_rate,
                "best_model": best_model,
                "best_model_accuracy": best_model_accuracy,
            },
            "top_trades": top_trades,
            "ai_model_rankings": ai_rankings,
            "trader_rankings": trader_rankings,
        }
    finally:
        db.close()


# ── Trade Journal ──────────────────────────────────────────────────────────────

class JournalEntryCreate(BaseModel):
    entry_type: str                       # opportunity | trade | copy_signal | manual
    source_id: Optional[int] = None
    market_id: Optional[str] = ""
    question: str
    side: str
    price: Optional[float] = None
    edge_pct: Optional[float] = None
    confidence: Optional[str] = None
    notes: Optional[str] = ""
    tags: Optional[list] = []
    rating: Optional[int] = None          # 1–5


class JournalEntryUpdate(BaseModel):
    notes: Optional[str] = None
    tags: Optional[list] = None
    rating: Optional[int] = None


def _serialize_journal(row) -> dict:
    tags = []
    try:
        tags = json.loads(row.tags) if row.tags else []
    except Exception:
        pass
    return {
        "id": row.id,
        "entry_type": row.entry_type,
        "source_id": row.source_id,
        "market_id": row.market_id,
        "question": row.question,
        "side": row.side,
        "price": row.price,
        "edge_pct": row.edge_pct,
        "confidence": row.confidence,
        "notes": row.notes,
        "tags": tags,
        "rating": row.rating,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@app.post("/api/journal")
async def create_journal_entry(req: JournalEntryCreate):
    db = SessionLocal()
    try:
        entry = JournalEntry(
            entry_type=req.entry_type,
            source_id=req.source_id,
            market_id=req.market_id,
            question=req.question,
            side=req.side,
            price=req.price,
            edge_pct=req.edge_pct,
            confidence=req.confidence,
            notes=req.notes or "",
            tags=json.dumps(req.tags or []),
            rating=req.rating,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        logger.info(f"Journal entry created: {entry.entry_type} — {entry.question[:60]}")
        return {"id": entry.id, "ok": True}
    finally:
        db.close()


@app.get("/api/journal")
async def list_journal_entries(limit: int = 100, entry_type: str = "", rating: int = 0):
    db = SessionLocal()
    try:
        q = db.query(JournalEntry)
        if entry_type:
            q = q.filter(JournalEntry.entry_type == entry_type)
        if rating > 0:
            q = q.filter(JournalEntry.rating >= rating)
        rows = q.order_by(JournalEntry.created_at.desc()).limit(limit).all()
        return [_serialize_journal(r) for r in rows]
    finally:
        db.close()


@app.put("/api/journal/{entry_id}")
async def update_journal_entry(entry_id: int, req: JournalEntryUpdate):
    db = SessionLocal()
    try:
        entry = db.query(JournalEntry).filter(JournalEntry.id == entry_id).first()
        if not entry:
            raise HTTPException(404, "Journal entry not found")
        if req.notes is not None:
            entry.notes = req.notes
        if req.tags is not None:
            entry.tags = json.dumps(req.tags)
        if req.rating is not None:
            entry.rating = req.rating
        entry.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.delete("/api/journal/{entry_id}")
async def delete_journal_entry(entry_id: int):
    db = SessionLocal()
    try:
        entry = db.query(JournalEntry).filter(JournalEntry.id == entry_id).first()
        if not entry:
            raise HTTPException(404, "Journal entry not found")
        db.delete(entry)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ── AI Assistant ───────────────────────────────────────────────────────────────

class AssistantChatRequest(BaseModel):
    message: str
    session_id: str


@app.post("/api/assistant/chat")
async def assistant_chat(req: AssistantChatRequest):
    if not _assistant:
        raise HTTPException(status_code=503, detail="AI assistant not available — OPENROUTER_API_KEY not configured")

    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(message) > 2000:
        raise HTTPException(status_code=400, detail="Message too long (max 2000 characters)")

    # Fetch real on-chain account data; fall back gracefully if wallet not configured
    try:
        profile, recent_trades, open_positions = await asyncio.gather(
            _poly_client.get_my_profile(),
            _poly_client.get_my_trade_history(limit=50),
            _poly_client.get_my_open_positions(),
            return_exceptions=True,
        )
        if isinstance(profile, Exception):
            profile = None
        if isinstance(recent_trades, Exception):
            recent_trades = []
        if isinstance(open_positions, Exception):
            open_positions = []
    except Exception:
        profile, recent_trades, open_positions = None, [], []

    db = SessionLocal()
    try:
        opportunities = [
            {
                "question": o.question,
                "side": o.side,
                "edge_pct": o.edge_pct,
                "confidence": o.confidence,
                "volume_24h": o.volume_24h,
                "yes_price": o.yes_price,
                "no_price": o.no_price,
            }
            for o in db.query(MarketOpportunity)
            .filter(MarketOpportunity.outcome.is_(None))
            .order_by(MarketOpportunity.edge_pct.desc())
            .limit(10)
            .all()
        ]
        journal_entries = [
            _serialize_journal(j)
            for j in db.query(JournalEntry)
            .order_by(JournalEntry.created_at.desc())
            .limit(10)
            .all()
        ]
    finally:
        db.close()

    context = build_platform_context(profile, recent_trades, open_positions, opportunities, bot_state, journal_entries)

    try:
        reply, remaining = await _assistant.chat(
            session_id=req.session_id,
            message=message,
            platform_context=context,
        )
        return {"reply": reply, "rate_limit_remaining": remaining}
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc))


@app.delete("/api/assistant/session/{session_id}")
async def assistant_clear_session(session_id: str):
    if _assistant:
        _assistant.clear_session(session_id)
    return {"status": "cleared"}


# ── WebSocket ──────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    await ws.send_json({"event": "connected", "status": bot_state})
    try:
        while True:
            data = await ws.receive_text()
            # Handle ping
            if data == "ping":
                await ws.send_json({"event": "pong"})
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
