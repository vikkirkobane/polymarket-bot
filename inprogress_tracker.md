# In-Progress Tracker

## Consensus Page Fixes — 2026-05-17 / 2026-05-18

### Bug 1: Approved cards reappearing after 30-second auto-refresh
**Status:** Fixed  
**Root cause:** `approveConsensusTrade` only dimmed the card in the DOM but never called the server-side dismiss endpoint. On the next 30-second auto-refresh, `loadConsensus()` rebuilt the list from server data and the approved card came back undimmed.  
**Fix (`frontend/app.js`):**
- After a successful approval, call `POST /api/consensus-trades/{market_id}/dismiss` to suppress the entry server-side.
- Set `_consensusData[idx] = null` so it is excluded from any in-memory re-renders.
- Animate the card out (fade + slide left) instead of just dimming it, matching the Dismiss flow.

---

### Bug 2: Win/Loss rating buttons silently doing nothing
**Status:** Fixed  
**Root cause:** `markOutcomeByMarket` posts to `POST /api/outcomes/by-market`, which looked up a `MarketOpportunity` row by market ID. Consensus cards come from live trader positions, not AI scans, so no such row ever exists — the backend returned 404, the `post()` helper threw, and the catch block only logged to console with no user feedback.  
**Fix (`backend/main.py`):**
- Removed the `raise HTTPException(404, ...)` path.
- When no `MarketOpportunity` is found, upsert a minimal stub row (`model_used="consensus"`) with the provided question text and mark the outcome on it.
- Added optional `question: str = ""` field to `MarketOutcomeRequest` so the stub gets a human-readable label.

**Fix (`frontend/app.js`):**
- `markOutcomeByMarket` now accepts a `question` parameter and forwards it to the backend.
- Consensus card outcome buttons pass `escAttr(c.question)` as the fourth argument.
- Removed the special 404 toast (no longer reachable); generic error toast kept for real failures.

---

### Bug 3: Loading spinner flashing every 30 seconds
**Status:** Fixed  
**Root cause:** `loadConsensus()` always reset the list HTML to a loading spinner, including on the 30-second auto-refresh, causing a disruptive flash while the user was viewing cards.  
**Fix (`frontend/app.js`):**
- Added `silent = false` parameter to `loadConsensus`.
- Auto-refresh interval now calls `loadConsensus(true)` — updates stats bar and data in the background without blanking the list.
- Manual "Refresh" button still calls `loadConsensus()` (silent=false) so the spinner shows when explicitly requested.

---

### Bug 4: Resolved/expired markets appearing in the consensus list
**Status:** Fixed  
**Root cause:** `get_trader_positions` in `client.py` returned all positions including those from closed, redeemed, or already-settled markets. The consensus engine had no filtering for stale positions.  
**Fix (`backend/polymarket/client.py`):**
- Skip positions where `redeemed` or `redeemedAt` is set (trader already cashed out).
- Skip positions where `closed` or `closedAt` is set (market explicitly closed).
- Skip positions where `endDate` (any common field name variant) has passed relative to UTC now.
- Skip positions where `curPrice` is exactly `0.0` or `1.0` — these are on-chain settled markets with prices pinned at resolution.
- Fixed a subtle bug: replaced `float(raw) or 0.5` with an explicit `None` check so a raw price of `0` is preserved as `0.0` rather than being overridden to `0.5`.

**To apply immediately:** click **"Force Reload Positions"** on the consensus page to bypass the 5-minute server cache and fetch fresh filtered data.

---

## Files Modified

| File | Changes |
|---|---|
| `frontend/app.js` | `loadConsensus` silent mode; approve flow calls dismiss + animates out; `markOutcomeByMarket` accepts + forwards question |
| `backend/main.py` | `/api/outcomes/by-market` upserts stub `MarketOpportunity` instead of 404ing |
| `backend/polymarket/client.py` | `get_trader_positions` filters closed/redeemed/expired/settled positions |
