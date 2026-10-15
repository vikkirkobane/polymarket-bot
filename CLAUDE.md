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
pytest tests/test_core.py::TestQuickEdge::test_normal_market_passes
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
- `GAMMA_API` — market listings with CLOB token ids, best bid/ask, spread (public)
- `DATA_API` — **v2 routes only** (`/v2/positions`, `/v2/leaderboard`, `/v2/user-stats`, `/v2/activity`). v1 retired 2026-10-24; v2 wraps payloads in `{"data": ..., "pagination": ...}` and uses snake_case. `_extract()` unwraps envelopes. The old `GET /users/{address}` is dead (404).
- `CLOB_API` — public `/price` reads; live orders go through the official `polymarket-client` SDK (`AsyncSecureClient.place_market_order`), NOT hand-rolled HMAC. Orders take the outcome **token id**, never the condition id (`resolve_token_id` maps condition+side → token).

**`backend/polymarket/researcher.py`** — `MarketResearcher.run_scan()` orchestrates a full scan cycle: fetch markets → quick-edge pre-filter → AI analysis → trader alignment check → upsert to DB. Trader positions are fetched in parallel (semaphore) and cached for 5 minutes (`_TRADER_POSITIONS_TTL`). `_quick_edge` drops near-resolved markets (<0.03 / >0.97) and non-orderbook markets, and scores spread/deviation/volume/liquidity.

**`backend/analyst/`** — Abstract `BaseAnalyst` with concrete implementations for Claude, OpenAI, Gemini, OpenRouter, and Mock. `get_analyst(model, settings)` in `factory.py` is the factory. The base class handles JSON parsing and repair for truncated responses. Analysis is chunked (10 markets per call, max 3 concurrent); the model returns only `estimated_prob` + `confidence` + `reasoning` — **edge math is derived in code** against executable prices (buy YES at best ask, buy NO at 1 − best bid), never trusted from the LLM. OpenRouter has a fallback chain plus adaptive `max_tokens` that parses 402 "can only afford N tokens" budget errors and retries.

**`backend/polymarket/copy_trader.py`** — `CopyTradingEngine` polls trader positions concurrently, diffs against `TraderPositionState` (side + `last_event_ts` persisted in DB). First poll for a trader only seeds state (no signal flood after restart); signals fire on new markets, side flips, or size adds (`last_event_ts` advanced), and are deduped per (trader, market, side) for 24h.

**`backend/trader/executor.py`** — Paper and live trade execution. Enforces per-trade and daily spend caps. Updates `Trade` and `Position` tables. Closing an opposite-side position exits at `1 − trade_price` (binary complement), and paper balance is credited with sale proceeds. `Position.market_id` is UNIQUE, so deletes are flushed before inserts.

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

`AI_MODEL` env var accepts: `claude`, `openai`, `gemini`, `openrouter`, `mock`. The `mock` analyst returns random opportunities without any API call — useful for testing the full pipeline. Specific models can be overridden with `ANTHROPIC_MODEL` / `OPENAI_MODEL` / `GEMINI_MODEL` (defaults: `claude-sonnet-4-5`, `gpt-4o`, `gemini-3-flash`).

### Live trading

Switching to live via `POST /api/bot/mode` validates prerequisites (POLY_PRIVATE_KEY set, `polymarket` SDK installed, wallet address resolvable) and rejects with 400 otherwise. Live orders place through the SDK using CLOB token ids stored on opportunities (`yes_token_id`/`no_token_id`) or resolved on demand via `resolve_token_id`.

### Trade approval flow

When `AUTO_EXECUTE=false` (default), high-conviction opportunities are flagged (`flagged_for_approval=True`) in `market_opportunities` and broadcast as `trade_alert` WebSocket events. The dashboard shows a 30-minute approval window; after that the flag auto-expires. Approving executes via `TradeExecutor`.
