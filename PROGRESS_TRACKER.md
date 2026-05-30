# Polymarket Bot — Progress Tracker

## Feature: Outcome Rating & Rankings Page
**Status:** Complete  
**Session:** May 2026

---

### Overview
Added win/loss outcome rating buttons across three pages and a new Rankings page to track AI trade performance and copy trader accuracy.

---

### Changes by File

#### `backend/database.py`
- Added `outcome` (VARCHAR nullable) and `outcome_marked_at` (DATETIME nullable) columns to `MarketOpportunity`
- Added `flagged_for_approval` (BOOLEAN default False) column to `MarketOpportunity`
- Added `outcome` and `outcome_marked_at` columns to `CopySignal`
- SQLite ALTER TABLE migrations added to `init_db()` so existing DBs get columns automatically

#### `backend/main.py`
- **`select_and_process_opportunities()`** — when `auto_execute=False`, looks up the opportunity in DB and sets `flagged_for_approval=True` + stamps `scanned_at=now` before broadcasting WebSocket alert (makes approvals persist across reloads; 30-min window starts from alert time)
- **`GET /api/pending-trades/dashboard`** — returns all flagged research opportunities + pending copy signals created within the last 30 minutes; auto-unflagged records older than 30 min on each call; no record limit
- **`POST /api/pending-trades/research/{opp_id}/dismiss`** — sets `flagged_for_approval=False`
- **`POST /api/pending-trades/signal/{signal_id}/dismiss`** — sets signal `status='skipped'`
- **`POST /api/opportunities/{opp_id}/outcome`** — marks outcome by DB id (Opportunities / Best Picks pages)
- **`POST /api/outcomes/by-market`** — marks outcome by `market_id` (Dashboard, where only market_id is available from WebSocket)
- **`POST /api/copy-signals/{signal_id}/outcome`** — marks outcome for a copy signal
- **`GET /api/rankings?time_filter=all|30d|7d`** — returns combined stats from both `market_opportunities` and `copy_signals`: `stats`, `top_trades` (success outcomes from both tables merged, sorted by quality score), `ai_model_rankings`, `trader_rankings`

#### `backend/polymarket/researcher.py`
- `get_latest_opportunities()` returns `outcome` and `outcome_marked_at` fields so existing ratings show on page load
- `_save_opportunities()` upsert intentionally skips outcome fields so rescans don't overwrite user ratings

#### `frontend/index.html`
- Rankings nav item added (`id="nav-rankings"`, icon 🏆)
- Full Rankings page added with: time filter tabs (All Time / Last 30 Days / Last 7 Days), 4 stat cards, two-column layout (Top Winning Trades left, AI Model Performance + Trader Leaderboard right)
- Refresh button added to Dashboard Pending Approvals card header
- Cache versioned: `styles.css?v=5`, `app.js?v=9`

#### `frontend/app.js`
- **Pending approvals persistence** — replaced in-memory `pendingTrades` array flow with DB-backed `loadDashboardPendingTrades()` that fetches `/api/pending-trades/dashboard`; called on page load, after approve/dismiss, and on WebSocket `trade_alert`
- **`renderPendingTrades()`** — shows full question text (no truncation); outcome rating buttons for both research (by market_id) and copy signals (by signal id); trader name badge on copy signal cards
- **`approveTrade()`** / **`dismissTrade()`** — now async, calls dismiss endpoints, then refreshes from DB
- **`renderOutcomeFeedback(oppId, currentOutcome)`** — ✓/✗ buttons or marked state for Opportunities and Best Picks pages
- **`markOutcome(oppId, outcome)`** — calls `/api/opportunities/{id}/outcome`
- **`markOutcomeByMarket(marketId, outcome)`** — calls `/api/outcomes/by-market`
- **`markSignalOutcome(signalId, outcome)`** — calls `/api/copy-signals/{id}/outcome`
- **`loadRankings(filter)`** — renders stat cards, Top Winning Trades (research + copy wins merged), AI Model table with accuracy bar, Trader Leaderboard with user-rated win/loss columns; copy trade cards show "Copy" badge + trader name instead of AI model badges
- Auto-refresh interval handles rankings page

#### `frontend/styles.css`
- Outcome button styles: `.outcome-feedback`, `.btn-outcome`, `.btn-outcome-success`, `.btn-outcome-failure`, `.outcome-marked`, `.outcome-marked.success`, `.outcome-marked.failure`
- Rankings layout: `.rankings-layout`, `.tab-group`, `.tab-btn`, `.rank-trades-list`, `.rank-trade-card`, `.rank-tier-gold`, `.rank-tier-silver`, `.rank-tier-bronze`, `.rank-tier-loss`
- Responsive breakpoint: `@media (max-width:1100px)` collapses rankings to single column

---

### Key Design Decisions

| Decision | Reason |
|---|---|
| Dual outcome endpoints (by-id and by-market-id) | WebSocket `trade_alert` events carry market_id but no DB id; Opportunities page has DB id directly |
| `flagged_for_approval` + `scanned_at` reset | Persists pending approvals to DB so they survive page reloads; timer resets to alert time not scan time |
| 30-minute window enforced server-side | Auto-unflag on each endpoint call keeps DB clean; no cron job needed |
| Copy signal wins merged into `top_trades` | All wins are in `copy_signals`; `market_opportunities` had zero successes at launch |
| `outcome` fields excluded from upsert | Prevents rescans from wiping user ratings |

---

### Bug Fixes

| Bug | Root Cause | Fix |
|---|---|---|
| Outcome buttons missing on Dashboard (research) | WebSocket event has no DB id; `opp.id` always undefined | Added `POST /api/outcomes/by-market` endpoint; dashboard uses market_id |
| Outcome buttons missing on Dashboard (copy signals) | Condition `isResearch && opp.id` skipped copy path entirely | Added separate copy signal outcome path using `_signal_id` |
| Pending approvals disappear on page refresh | `pendingTrades` was in-memory only | Added `flagged_for_approval` DB column; `loadDashboardPendingTrades()` fetches from DB on every load |
| Top Winning Trades section always empty | Rankings only queried `market_opportunities`; all wins were in `copy_signals` | Merged both tables into `top_trades` response |
| 30-minute window started too early | `scanned_at` set at scan time, not at alert/flag time | Reset `scanned_at = now` when setting `flagged_for_approval = True` |

---

## Feature: Consensus Page Bug Fixes
**Status:** Complete  
**Session:** May 2026

---

### Overview
Fixed four bugs on the Consensus Trades page: approved cards reappearing, Win/Loss buttons silently failing, disruptive loading spinner on auto-refresh, and resolved/expired markets showing in the list.

---

### Changes by File

#### `frontend/app.js`
- **`loadConsensus(silent = false)`** — added `silent` parameter; auto-refresh passes `true` so the list is updated in the background without blanking to a loading spinner
- **`approveConsensusTrade()`** — after a successful trade, now calls `POST /api/consensus-trades/{market_id}/dismiss` to suppress the card server-side, sets `_consensusData[idx] = null`, and animates the card out (fade + slide) instead of just dimming it; previously the card reappeared undimmed on the next 30-second refresh
- **`markOutcomeByMarket(marketId, outcome, buttonEl, question = "")`** — added `question` parameter, forwarded to backend; removed special 404 toast (no longer reachable after backend fix)
- **Consensus card outcome buttons** — now pass `escAttr(c.question)` as the fourth argument to `markOutcomeByMarket`

#### `backend/main.py`
- **`MarketOutcomeRequest`** — added optional `question: str = ""` field
- **`POST /api/outcomes/by-market`** — removed `raise HTTPException(404, ...)` when no `MarketOpportunity` exists; instead upserts a minimal stub row with `model_used="consensus"` and the provided question, then marks the outcome on it; consensus cards now save Win/Loss ratings correctly

#### `backend/polymarket/client.py`
- **`get_trader_positions()`** — added filtering to skip resolved/stale positions:
  - Skip if `redeemed` or `redeemedAt` is set
  - Skip if `closed` or `closedAt` is set
  - Skip if `endDate` (any common field name variant) has passed relative to UTC now
  - Skip if `curPrice` is exactly `0.0` or `1.0` (on-chain settled market)
  - Fixed `float(raw) or 0.5` bug: replaced with explicit `None` check so a raw price of `0` is preserved as `0.0` instead of being overridden to `0.5`

---

### Bug Fixes

| Bug | Root Cause | Fix |
|---|---|---|
| Approved consensus cards reappear after 30s | `approveConsensusTrade` dimmed card in DOM only; never called dismiss endpoint; next auto-refresh rebuilt full list from server | Call dismiss endpoint after approval; null out `_consensusData[idx]`; animate card out |
| Win/Loss buttons did nothing on consensus cards | Consensus cards have no `MarketOpportunity` row (positions come from trader API, not AI scans); backend returned 404; catch block only logged | Upsert stub `MarketOpportunity` row on first rating; pass question text from frontend |
| Loading spinner flashed every 30 seconds | `loadConsensus()` always reset list HTML to spinner, including silent background refreshes | Added `silent` flag; auto-refresh uses `loadConsensus(true)` |
| Resolved/expired markets in consensus list | `get_trader_positions` returned all positions including closed, redeemed, and on-chain settled ones | Filter on `closed`, `redeemed`, past `endDate`, and `curPrice == 0 or 1` |
