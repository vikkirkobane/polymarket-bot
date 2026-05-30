# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the server (hot-reload enabled by default)
python run.py

# Run without hot-reload
uvicorn backend.main:app --host 0.0.0.0 --port 8000

# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest

# Run a single test file
pytest tests/test_core.py

# Run a single test class or method
pytest tests/test_core.py::TestQuickEdge
pytest tests/test_core.py::TestQuickEdge::test_extreme_price_gets_boost
```

## Architecture

This is a FastAPI + Vanilla JS SPA. The server serves the frontend as static files and exposes a REST + WebSocket API. All state lives in SQLite (`data/trades.db`).

### Request flow

```
Browser → GET / → FileResponse(frontend/index.html)
Browser → REST  → backend/main.py routes
Browser → WS    → /ws → ws_manager.broadcast() pushes events to all clients
```

The bot loop runs as an asyncio background task (`bot_loop()` in `main.py`). It fires `run_scan_cycle()` and `run_copy_cycle()` on their respective schedules. Both cycles broadcast results over WebSocket so the UI updates without polling.

### Key modules

**`backend/main.py`** — All FastAPI routes and the lifespan context manager. Global singletons (`_poly_client`, `_analyst`, `_executor`, `_portfolio`, `_copy_engine`, `_researcher`, `_assistant`) are initialized in `lifespan()` and held as module-level globals. Routes access them directly.

**`backend/polymarket/client.py`** — Three Polymarket API surfaces:
- `GAMMA_API` — market listings (public, no auth)
- `DATA_API` — trader stats, positions, leaderboard (public, no auth)
- `CLOB_API` — order execution (HMAC-signed with L2 credentials)

**`backend/polymarket/researcher.py`** — `MarketResearcher.run_scan()` orchestrates a full scan cycle: fetch markets → quick-edge pre-filter → AI analysis → trader alignment check → upsert to DB. Trader positions are cached for 5 minutes (`_TRADER_POSITIONS_TTL`).

**`backend/analyst/`** — Abstract `BaseAnalyst` with concrete implementations for Claude, OpenAI, Gemini, OpenRouter, and Mock. `get_analyst(model, settings)` in `factory.py` is the factory. The base class handles JSON parsing and repair for truncated responses. OpenRouter has a built-in fallback sequence through 7+ free models.

**`backend/polymarket/copy_trader.py`** — `CopyTradingEngine` polls trader positions on each cycle, diffs against `TraderPositionState` (persisted in DB to survive restarts), and generates `CopySignal` records for new positions.

**`backend/trader/executor.py`** — Paper and live trade execution. Enforces per-trade and daily spend caps. Updates `Trade` and `Position` tables.

**`backend/database.py`** — All SQLAlchemy models. `init_db()` uses `ALTER TABLE` try/except blocks for additive column migrations — no migration tool.

**`backend/config.py`** — All settings via `pydantic-settings` from `.env`. Cached with `@lru_cache`. Env var names are the `alias` fields (uppercase).

**`backend/assistant.py`** — `AssistantService` wraps OpenRouter for the floating chat widget. Has per-session message history and rate limiting.

### Database models

| Table | Purpose |
|---|---|
| `trades` | Every executed trade (paper + live) |
| `positions` | Current open positions with avg price + P&L |
| `copy_traders` | Followed wallet addresses + their stats |
| `copy_signals` | Generated copy trade signals (pending/executed/skipped) |
| `trader_position_states` | Last-seen positions per trader (for delta detection) |
| `market_opportunities` | AI scan results; includes `outcome`, `flagged_for_approval` |
| `journal_entries` | Manual user notes on trades |

### Frontend

Single-page app in `frontend/`. No build step — plain JS served directly. `app.js` opens a WebSocket on load and updates the UI on events. Page routing is hash-based (`#dashboard`, `#opportunities`, etc.).

### Dependency constraint

FastAPI 0.111.0 requires `starlette==0.37.2` exactly. Starlette 1.x removed the `on_startup` parameter that FastAPI 0.111.0 passes internally. Both are pinned in `requirements.txt` — do not upgrade either independently.

### AI model names

`AI_MODEL` env var accepts: `claude`, `openai`, `gemini`, `openrouter`, `mock`. The `mock` analyst returns random opportunities without any API call — useful for testing the full pipeline.

### Trade approval flow

When `AUTO_EXECUTE=false` (default), high-conviction opportunities are flagged (`flagged_for_approval=True`) in `market_opportunities` and broadcast as `trade_alert` WebSocket events. The dashboard shows a 30-minute approval window; after that the flag auto-expires. Approving executes via `TradeExecutor`.
