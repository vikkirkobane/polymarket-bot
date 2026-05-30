# PolyBot — Polymarket Trading Bot & Command Center

A full-stack automated trading system for [Polymarket](https://polymarket.com) with a web-based Command Center dashboard. Supports **copy trading** top performers and **AI-powered market research** using Claude, GPT-4o, or Gemini.

---

## Features

- **Command Center Dashboard** — Dark-themed web UI to monitor and control the bot in real time
- **Copy Trading Engine** — Auto-discovers top traders from the Polymarket leaderboard and mirrors their positions (scaled to your bankroll)
- **AI Research Engine** — Scans active markets and uses your chosen AI model to detect mis-priced bets
- **Multi-Model AI Support** — Switch between Claude (Anthropic), GPT-4o (OpenAI), and Gemini (Google) from the UI
- **Paper & Live Trading** — Safely simulate trades before going live; toggle modes from the dashboard
- **Real-Time WebSocket Feed** — Live updates on trades, signals, and scans without page refresh
- **Full Trade History & P&L** — SQLite database tracks every trade, position, and daily P&L chart

---

## Project Structure

```
polymarket-bot/
├── backend/
│   ├── main.py                  # FastAPI server (REST API + WebSocket)
│   ├── config.py                # Settings loaded from .env
│   ├── database.py              # SQLAlchemy models (SQLite)
│   ├── polymarket/
│   │   ├── client.py            # Gamma + CLOB + Data API client
│   │   ├── copy_trader.py       # Copy trading engine
│   │   └── researcher.py        # AI market research engine
│   ├── analyst/
│   │   ├── base.py              # Abstract analyst + shared prompt
│   │   └── factory.py           # Claude / OpenAI / Gemini implementations
│   └── trader/
│       ├── executor.py          # Paper + live order execution
│       └── portfolio.py         # P&L and position tracking
├── frontend/
│   ├── index.html               # Command Center SPA
│   ├── styles.css               # Dark dashboard design
│   └── app.js                   # JavaScript app + WebSocket client
├── data/
│   └── trades.db                # SQLite database (auto-created)
├── polymarket_bot_main.py       # Original CLI entry point (preserved)
├── run.py                       # Server launcher
├── requirements.txt
└── .env.example                 # Template for your configuration
```

---

## Prerequisites

- **Python 3.10+** (tested on Python 3.13)
- At least one AI provider API key:
  - [Anthropic (Claude)](https://console.anthropic.com/)
  - [OpenAI (GPT-4o)](https://platform.openai.com/)
  - [Google (Gemini)](https://aistudio.google.com/)
- A [Polymarket](https://polymarket.com) account with API credentials for live trading (not required for paper trading)

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/your-username/polymarket-bot.git
cd polymarket-bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> **Python 3.13 users:** SQLAlchemy 2.0.36+ is required and already pinned in `requirements.txt`. If you see a `TypeError` related to `FastIntFlag`, run `pip install "sqlalchemy>=2.0.36" --upgrade`.

### 3. Configure environment

Copy the example file and fill in your credentials:

```bash
cp .env.example .env
```

Then open `.env` and set your values:

```env
# ── Trading Mode ───────────────────────────────────────────────────────────────
PAPER_MODE=true          # Set to false only when ready for live trading
PAPER_BALANCE=10000.0    # Virtual USD for paper trading simulation

# ── AI Model ───────────────────────────────────────────────────────────────────
AI_MODEL=claude          # Options: claude | openai | gemini | mock
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza...

# ── Polymarket (required for live trading only) ─────────────────────────────────
POLY_API_KEY=
POLY_API_SECRET=
POLY_PASSPHRASE=
POLY_PRIVATE_KEY=

# ── Copy Trading ───────────────────────────────────────────────────────────────
COPY_TRADING_ENABLED=true
COPY_SCALE_FACTOR=0.1    # Copy 10% of the original trader's bet size
MAX_COPY_TRADERS=10

# ── Risk Management ────────────────────────────────────────────────────────────
MIN_EDGE_PERCENT=5.0     # Minimum AI-detected edge to trade
MAX_BET_SIZE=100.0       # Maximum USD per trade
MAX_DAILY_SPEND=500.0    # Maximum USD per day
MIN_VOLUME=1000.0        # Minimum 24h market volume to consider

# ── Scheduling ─────────────────────────────────────────────────────────────────
SCAN_INTERVAL_MINUTES=15
COPY_POLL_INTERVAL_MINUTES=5
```

### 4. Launch the Command Center

```bash
python run.py
```

Then open your browser at: **http://localhost:8000**

---

## Using the Command Center

### Dashboard
The home screen shows your portfolio balance, total P&L, win rate, trade count, and a cumulative P&L chart. The live feed on the right updates in real time via WebSocket as trades fire and scans complete.

### Copy Traders
- Click **Refresh Leaderboard** to auto-import the top 20 Polymarket traders by profit
- Click **+ Add Wallet** to manually track any specific wallet address
- Remove traders at any time; the bot will stop copying them immediately

### Opportunities
- Select your AI model from the dropdown (Claude / GPT-4o / Gemini)
- Click **Run AI Scan** to fetch active markets and have the AI analyze them for edges
- Each opportunity card shows the question, YES/NO prices, edge %, confidence level, and the AI's reasoning

### Trade History
- Full paginated log of all trades
- Filter by source: **AI Research**, **Copy Trading**, or **Manual**

### Settings
- Toggle between **Paper Trading** (safe, simulated) and **Live Trading** (real money — requires Polymarket credentials)
- Switch AI models on the fly without restarting the server

---

## Bot Controls

| Button | Action |
|---|---|
| **Start Bot** | Begins the automated loop — research scans + copy trading polls run on schedule |
| **Stop Bot** | Gracefully halts the bot loop; no new trades will be placed |
| **Run AI Scan** | Triggers a one-off market scan immediately |
| **Refresh Leaderboard** | Re-fetches the top Polymarket traders and updates the DB |

---

## AI Models

| Model | Provider | Set `AI_MODEL=` |
|---|---|---|
| Claude 3.5 Sonnet | Anthropic | `claude` |
| GPT-4o | OpenAI | `openai` |
| Gemini 1.5 Pro | Google | `gemini` |
| Mock (no API key needed) | Built-in | `mock` |

The `mock` model is useful for testing the full pipeline without spending API credits — it returns randomly generated opportunities.

---

## Getting Polymarket API Credentials (Live Trading)

1. Visit [https://polymarket.com](https://polymarket.com) and connect your wallet
2. Go to **Settings → API Keys** and generate a CLOB API key
3. Copy the **API Key**, **Secret**, **Passphrase**, and your wallet's **Private Key** into `.env`
4. In the Command Center, switch mode to **Live** (you will be prompted to confirm)

> **Warning:** Live mode places real orders with real money. Start with paper mode and thoroughly review AI signals before going live.

---

## Safety Features

- **Paper mode is the default** — no real money is ever touched unless you explicitly switch to live
- **Live mode confirmation prompt** in the UI requires a deliberate action to activate
- **Per-trade and daily spend caps** enforced by `MAX_BET_SIZE` and `MAX_DAILY_SPEND`
- **Minimum edge threshold** prevents low-confidence trades from executing

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `UnicodeEncodeError` on startup | Your terminal uses cp1252. The code is already fixed — update to the latest version. |
| `TypeError: Can't replace canonical symbol` | Run `pip install "sqlalchemy>=2.0.36" --upgrade` |
| Dashboard shows blank / not loading | Ensure the server is running on port 8000 and check the terminal for errors |
| AI scan returns no opportunities | Try lowering `MIN_EDGE_PERCENT` in `.env` or use `AI_MODEL=mock` to test the pipeline |
| Copy traders list is empty | Click **Refresh Leaderboard** — the leaderboard endpoint may need a retry |

---

## Development

The server runs with hot-reload enabled by default (`uvicorn --reload`). Any changes to `.py` files will automatically restart the server.

To run without hot-reload (production-like):

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

---

## License

MIT — use at your own risk. This is not financial advice.
