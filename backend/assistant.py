"""backend/assistant.py — AI trading assistant via OpenRouter with rate limiting."""

import time
import logging
from collections import deque

import openai

logger = logging.getLogger(__name__)

# ── Rate Limiter ────────────────────────────────────────────────────────────────

class RateLimiter:
    """Sliding window rate limiter: N requests per window_seconds per session."""

    def __init__(self, max_requests: int = 12, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._sessions: dict[str, deque] = {}

    def check(self, session_id: str) -> tuple[bool, int]:
        """Returns (allowed, remaining). Cleans expired timestamps."""
        now = time.monotonic()
        cutoff = now - self.window_seconds

        if session_id not in self._sessions:
            self._sessions[session_id] = deque()

        ts = self._sessions[session_id]
        while ts and ts[0] < cutoff:
            ts.popleft()

        remaining = self.max_requests - len(ts)
        if remaining <= 0:
            return False, 0

        ts.append(now)
        return True, remaining - 1

    def cleanup_old_sessions(self):
        now = time.monotonic()
        cutoff = now - self.window_seconds * 10
        dead = [sid for sid, ts in self._sessions.items() if not ts or ts[-1] < cutoff]
        for sid in dead:
            del self._sessions[sid]


# ── Session Manager ─────────────────────────────────────────────────────────────

class SessionManager:
    """In-memory per-session conversation history."""

    MAX_HISTORY = 20
    SESSION_TTL = 1800  # 30 minutes

    def __init__(self):
        self._sessions: dict[str, dict] = {}

    def get_history(self, session_id: str) -> list[dict]:
        session = self._sessions.get(session_id)
        if not session:
            return []
        session["last_active"] = time.monotonic()
        return list(session["messages"])

    def add_message(self, session_id: str, role: str, content: str):
        if session_id not in self._sessions:
            self._sessions[session_id] = {
                "messages": deque(maxlen=self.MAX_HISTORY),
                "last_active": time.monotonic(),
            }
        self._sessions[session_id]["messages"].append({"role": role, "content": content})
        self._sessions[session_id]["last_active"] = time.monotonic()

    def clear(self, session_id: str):
        self._sessions.pop(session_id, None)

    def cleanup_expired(self):
        now = time.monotonic()
        dead = [sid for sid, s in self._sessions.items() if now - s["last_active"] > self.SESSION_TTL]
        for sid in dead:
            del self._sessions[sid]


# ── System Prompt ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert AI trading assistant embedded in a Polymarket prediction market trading bot dashboard.

## Your Role
You help the user make smarter, faster trading decisions by:
- Analyzing specific trades, market opportunities, and open positions
- Explaining AI-discovered edges, confidence levels, and why a bet is compelling
- Assessing risk/reward for specific markets and recommending position sizing
- Interpreting market probabilities vs the true probability you estimate
- Explaining copy trading signals and evaluating whether followed traders are worth trusting
- Reviewing portfolio performance and diagnosing patterns (win rate, average edge, model accuracy)
- Answering questions about prediction market mechanics, Polymarket specifics, and probability theory

## Polymarket Concepts You Know
- **YES/NO prices**: Implied probabilities. A YES price of 0.65 = 65% market-implied chance of YES outcome
- **Edge %**: How much the AI estimates the true probability differs from the market price (higher = better)
- **Confidence**: low/medium/high — reflects information quality, market liquidity, and event clarity
- **Copy trading**: Mirroring wallets with strong ROI/win-rate on Polymarket's on-chain data
- **CLOB**: Central Limit Order Book — Polymarket's trading infrastructure
- **Paper mode**: Simulated trades with virtual money for safe testing
- **Live mode**: Real USD trades on Polymarket — real financial risk

## Platform Context (live data)
{platform_context}

## Response Guidelines
- Be direct and specific — the user is making real financial decisions
- Reference exact numbers from the context data when relevant
- Call out risk factors clearly without being alarmist
- For analysis, use concise bullet points
- If you lack sufficient data to give a confident answer, say so explicitly
- Never fabricate market data — only use what's provided in context
- Keep responses tight — 2-5 sentences for simple questions, structured lists for analysis"""


def build_platform_context(
    profile: dict,
    recent_trades: list,
    open_positions: list,
    opportunities: list,
    bot_state: dict,
    journal_entries: list = None,
) -> str:
    """
    Build context injected into every assistant request.
    Computes the same derived stats as the My Profile page so the AI sees
    exactly what the user sees: capital deployed, unrealized P&L, position
    health, trade bias, most-traded markets, and on-chain account metrics.
    """
    lines: list[str] = []

    # ── Bot state ──
    lines.append(
        f"Bot mode: {bot_state.get('mode','paper').upper()} | "
        f"Scanning model: {bot_state.get('ai_model','?')} | "
        f"Bot running: {bot_state.get('running', False)}"
    )

    # ── On-chain account metrics (from /api/me/profile) ──
    if profile and not profile.get("_no_history"):
        roi    = profile.get("roi", 0) or 0
        profit = profile.get("profit", 0) or 0
        volume = profile.get("volume", 0) or 0
        wr     = profile.get("win_rate", 0) or 0
        lines.append(
            f"\nPolymarket account — ROI: {roi:.1f}% | Realized P&L: ${profit:+,.2f} | "
            f"Volume: ${volume:,.0f} | Win rate: {wr:.1f}%"
        )
    elif profile and profile.get("_no_history"):
        lines.append("\nPolymarket account: wallet found but no trading history yet")
    else:
        lines.append("\nPolymarket account: not configured (POLY_PRIVATE_KEY not set)")

    # ── Open positions — same math as renderProfileStatCards / renderProfileInsights ──
    if open_positions:
        enriched = []
        for p in open_positions:
            avg  = p.get("avg_price", 0.5) or 0.5
            cur  = p.get("current_price", 0.5) or 0.5
            size = p.get("size", 0) or 0
            unreal = (cur - avg) * (size / avg)
            enriched.append({**p, "unreal": unreal})

        total_deployed = sum(p.get("size", 0) for p in open_positions)
        total_unreal   = sum(p["unreal"] for p in enriched)
        winning        = sum(1 for p in enriched if p["unreal"] > 0)
        losing         = sum(1 for p in enriched if p["unreal"] < 0)
        yes_positions  = [p for p in open_positions if p.get("side") == "YES"]
        no_positions   = [p for p in open_positions if p.get("side") == "NO"]
        yes_exposure   = sum(p.get("size", 0) for p in yes_positions)
        no_exposure    = sum(p.get("size", 0) for p in no_positions)

        best  = max(enriched, key=lambda p: p["unreal"])
        worst = min(enriched, key=lambda p: p["unreal"])

        lines.append(
            f"\nOpen positions ({len(open_positions)}) — "
            f"Capital deployed: ${total_deployed:,.2f} | "
            f"Unrealized P&L: ${total_unreal:+,.2f} | "
            f"Health: {winning} winning / {losing} losing"
        )
        lines.append(
            f"  YES exposure: ${yes_exposure:,.2f} ({len(yes_positions)} pos) | "
            f"NO exposure: ${no_exposure:,.2f} ({len(no_positions)} pos)"
        )
        lines.append(
            f"  Best position: '{best.get('question','?')[:50]}' "
            f"${best['unreal']:+.2f} unrealized"
        )
        if len(enriched) > 1:
            lines.append(
                f"  Worst position: '{worst.get('question','?')[:50]}' "
                f"${worst['unreal']:+.2f} unrealized"
            )
        lines.append("  All open positions:")
        for p in sorted(enriched, key=lambda x: x["unreal"], reverse=True):
            avg = p.get("avg_price", 0.5) or 0.5
            cur = p.get("current_price", 0.5) or 0.5
            lines.append(
                f"    - {p.get('side','?')} '{p.get('question','?')[:55]}' "
                f"avg={avg*100:.0f}¢ cur={cur*100:.0f}¢ size=${p.get('size',0):.0f} "
                f"P&L=${p['unreal']:+.2f}"
            )

    # ── Trade history — same stats as renderProfileInsights ──
    if recent_trades:
        buys       = sum(1 for t in recent_trades if t.get("action") == "BUY")
        sells      = sum(1 for t in recent_trades if t.get("action") == "SELL")
        yes_trades = sum(1 for t in recent_trades if t.get("side") == "YES")
        no_trades  = sum(1 for t in recent_trades if t.get("side") == "NO")
        total_vol  = sum(t.get("size", 0) for t in recent_trades)
        avg_trade  = total_vol / len(recent_trades) if recent_trades else 0
        buy_pct    = round(buys / len(recent_trades) * 100) if recent_trades else 0
        yes_pct    = round(yes_trades / len(recent_trades) * 100) if recent_trades else 0

        # Most-traded market by frequency
        mkt_count: dict[str, int] = {}
        for t in recent_trades:
            q = t.get("question", "")
            if q:
                mkt_count[q] = mkt_count.get(q, 0) + 1
        top_mkt = max(mkt_count, key=lambda k: mkt_count[k]) if mkt_count else None
        unique_mkts = len(mkt_count)

        lines.append(
            f"\nTrade history ({len(recent_trades)} trades) — "
            f"Volume: ${total_vol:,.2f} | Avg trade: ${avg_trade:,.2f}"
        )
        lines.append(
            f"  Buy/Sell: {buys} BUY / {sells} SELL ({buy_pct}% buys) | "
            f"Side bias: {yes_trades} YES / {no_trades} NO ({yes_pct}% YES)"
        )
        lines.append(f"  Unique markets traded: {unique_mkts}")
        if top_mkt:
            lines.append(
                f"  Most traded market ({mkt_count[top_mkt]}x): '{top_mkt[:60]}'"
            )
        lines.append("  Most recent trades:")
        for t in recent_trades[:8]:
            action = t.get("action", "")
            lines.append(
                f"    - {t.get('side','?')} {action} '{t.get('question','?')[:55]}' "
                f"${t.get('size',0):.0f} @ {t.get('price',0)*100:.0f}¢"
            )

    # ── AI-discovered opportunities from scanner ──
    if opportunities:
        high = [o for o in opportunities if o.get("confidence") == "high"][:4]
        if high:
            lines.append(f"\nBot AI-discovered opportunities (high confidence, unresolved):")
            for o in high:
                lines.append(
                    f"  - {o.get('side','?')} '{o.get('question','?')[:55]}' "
                    f"edge={o.get('edge_pct',0):.1f}% vol=${o.get('volume_24h',0):,.0f} "
                    f"YES={o.get('yes_price',0)*100:.0f}¢/NO={o.get('no_price',0)*100:.0f}¢"
                )

    # ── Trade Journal ──
    if journal_entries:
        lines.append(f"\nTrade Journal ({len(journal_entries)} most recent entries — user notes & ratings):")
        for j in journal_entries:
            tags_str = ", ".join(j.get("tags") or [])
            rating = j.get("rating")
            rating_str = f" | rating={rating}/5" if rating else ""
            edge = j.get("edge_pct")
            conf = j.get("confidence")
            meta = ""
            if edge:
                meta += f" | edge={edge:.1f}%"
            if conf:
                meta += f" | conf={conf}"
            if tags_str:
                meta += f" | tags: {tags_str}"
            lines.append(
                f"  - {j.get('side','?')} '{(j.get('question') or '')[:55]}' "
                f"[{j.get('entry_type','?')}]{meta}{rating_str}"
            )
            if j.get("notes"):
                lines.append(f"    Notes: \"{j['notes'][:200]}\"")

    return "\n".join(lines)


# ── Model Routing ────────────────────────────────────────────────────────────────

# Keywords that signal a complex analysis request → use the stronger model
_ANALYSIS_KEYWORDS = {
    "analyze", "analysis", "trade", "edge", "confidence", "risk", "portfolio",
    "position", "opportunity", "compare", "explain", "should i", "recommend",
    "strategy", "win rate", "p&l", "profit", "loss", "market", "probability",
    "copy", "signal", "best pick", "approve", "dismiss", "execute",
}

# Primary model for deep analysis, fallback sequence for availability
# (slugs verified live on OpenRouter 2026-10-15)
_ANALYSIS_MODELS = [
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/auto",
]

# Fast model for simple Q&A, fallback sequence
_FAST_MODELS = [
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/auto",
]

_EXTRA_HEADERS = {
    "HTTP-Referer": "https://github.com/polymarket-bot",
    "X-Title": "Polymarket Bot Assistant",
}


# ── Assistant Service ────────────────────────────────────────────────────────────

class AssistantService:
    """Core AI trading assistant powered by OpenRouter."""

    def __init__(self, api_key: str, preferred_model: str = ""):
        self.client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        self.rate_limiter = RateLimiter(max_requests=12, window_seconds=60)
        self.sessions = SessionManager()
        # If the user has configured a specific model, put it first in both lists
        if preferred_model and preferred_model not in ("openrouter", ""):
            self._analysis_models = [preferred_model] + [m for m in _ANALYSIS_MODELS if m != preferred_model]
            self._fast_models = [preferred_model] + [m for m in _FAST_MODELS if m != preferred_model]
        else:
            self._analysis_models = list(_ANALYSIS_MODELS)
            self._fast_models = list(_FAST_MODELS)

    def _pick_model_list(self, message: str) -> list[str]:
        lower = message.lower()
        if any(kw in lower for kw in _ANALYSIS_KEYWORDS) or len(message) > 120:
            return self._analysis_models
        return self._fast_models

    async def _call_with_fallback(self, models: list[str], messages: list[dict], system: str) -> tuple[str, str]:
        """Try each model in order. Returns (reply_text, model_used)."""
        last_err = None
        for model in models:
            try:
                response = await self.client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system}, *messages],
                    max_tokens=1024,
                    extra_headers=_EXTRA_HEADERS,
                )
                content = response.choices[0].message.content
                if content:
                    return content, model
            except Exception as exc:
                last_err = exc
                err_str = str(exc)
                # Retry with next model on availability errors
                if any(code in err_str for code in ("404", "503", "502", "429", "overloaded")):
                    logger.warning(f"[assistant] {model} unavailable ({err_str[:60]}), trying next")
                    continue
                raise
        raise ValueError(f"All OpenRouter models failed. Last error: {last_err}")

    async def chat(
        self,
        session_id: str,
        message: str,
        platform_context: str,
    ) -> tuple[str, int]:
        """
        Process a chat message and return (reply_text, rate_limit_remaining).
        Raises ValueError on rate limit or all-model failure.
        """
        allowed, remaining = self.rate_limiter.check(session_id)
        if not allowed:
            raise ValueError(
                "Rate limit reached (12 messages/min). Please wait a moment before sending."
            )

        history = self.sessions.get_history(session_id)
        history.append({"role": "user", "content": message})
        self.sessions.add_message(session_id, "user", message)

        system_text = SYSTEM_PROMPT.format(platform_context=platform_context)
        models = self._pick_model_list(message)

        try:
            reply, model_used = await self._call_with_fallback(models, history, system_text)
        except ValueError:
            raise
        except openai.RateLimitError:
            raise ValueError("OpenRouter rate limit hit. Please wait 30 seconds and try again.")
        except openai.APIStatusError as exc:
            logger.error(f"[assistant] OpenRouter API error: {exc}")
            raise ValueError(f"AI service error: {exc.message}")

        self.sessions.add_message(session_id, "assistant", reply)
        logger.info(f"[assistant] session={session_id[:8]} model={model_used}")
        return reply, remaining

    def clear_session(self, session_id: str):
        self.sessions.clear(session_id)
