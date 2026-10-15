"""backend/polymarket/client.py — Async Polymarket API client.

Data reads use Data API v2 (v1 is retired on 2026-10-24). v2 responses wrap
payloads in {"data": ..., "pagination": ...} and use snake_case fields.
Live order execution uses the official `polymarket-client` SDK (successor to
the archived py-clob-client) which handles EIP-712 order signing.
"""

import asyncio
import base64
import hashlib
import hmac as hmac_lib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"


def _extract(payload: Any) -> Any:
    """Return the payload from a Data API v2 envelope, or the bare body (v1)."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class PolymarketClient:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        private_key: str = "",
        wallet_address_override: str = "",
        timeout: float = 20.0,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.private_key = private_key
        self.timeout = timeout
        # Explicit override takes priority over derivation from private key
        self.wallet_address = wallet_address_override or self._derive_wallet_address(private_key)
        self._public_headers = {"Content-Type": "application/json"}
        self._http: Optional[httpx.AsyncClient] = None
        self._sdk_client = None  # lazily created polymarket AsyncSecureClient

    # ── HTTP plumbing ──────────────────────────────────────────────────────────

    async def _get_http(self) -> httpx.AsyncClient:
        """One shared AsyncClient per event loop (connection pooling, less overhead)."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self.timeout, headers=self._public_headers)
        return self._http

    async def aclose(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
        if self._sdk_client is not None:
            for closer in ("aclose", "close"):
                fn = getattr(self._sdk_client, closer, None)
                if callable(fn):
                    try:
                        result = fn()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        logger.debug(f"SDK client {closer}() failed: {exc}")
                    break
            self._sdk_client = None

    async def _get_json(self, url: str, params: dict | None = None) -> Any:
        """GET with light retry on transient 429/5xx."""
        client = await self._get_http()
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await client.get(url, params=params or {})
                if resp.status_code in (429, 500, 502, 503):
                    await asyncio.sleep(0.5 * (attempt + 1))
                    last_exc = httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError:
                raise
            except Exception as exc:  # network errors — retry
                last_exc = exc
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last_exc or RuntimeError(f"GET {url} failed")

    # ── Auth ───────────────────────────────────────────────────────────────────

    @staticmethod
    def _derive_wallet_address(private_key: str) -> str:
        if not private_key:
            return ""
        try:
            from eth_account import Account
            return Account.from_key(private_key).address
        except Exception as exc:
            logger.warning(f"Could not derive wallet address from private key: {exc}")
            return ""

    def _clob_headers(self, method: str, path: str, body: str = "") -> dict:
        """Build Polymarket CLOB L2 HMAC authentication headers (GET reads only)."""
        if not (self.api_key and self.api_secret and self.passphrase):
            raise RuntimeError("CLOB credentials (POLY_API_KEY / POLY_API_SECRET / POLY_PASSPHRASE) not set")

        timestamp = str(int(time.time()))
        nonce = "0"
        message = f"{timestamp}{method.upper()}{path}{body}"

        # api_secret is URL-safe base64 — pad and decode
        padding = "=" * (4 - len(self.api_secret) % 4)
        secret_bytes = base64.urlsafe_b64decode(self.api_secret + padding)

        sig = base64.b64encode(
            hmac_lib.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
        ).decode()

        return {
            "Content-Type": "application/json",
            "POLY_ADDRESS": self.wallet_address,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": nonce,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.passphrase,
        }

    # ── Markets ────────────────────────────────────────────────────────────────

    async def get_markets(self, limit: int = 100, min_volume: float = 1000.0) -> list[dict]:
        """Fetch active, order-book-enabled binary markets from the Gamma API.

        Includes CLOB token ids and top-of-book pricing so downstream code can
        estimate real executable prices (mid vs. ask) instead of last-trade mid.
        """
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
            "enableOrderBook": "true",
        }
        data = await self._get_json(f"{GAMMA_API}/markets", params)

        markets = data if isinstance(data, list) else _extract(data) or []
        result = []
        for m in markets:
            vol = _f(m.get("volume24hr"))
            if vol < min_volume:
                continue

            yes_price = no_price = 0.5

            raw_prices = m.get("outcomePrices")
            if isinstance(raw_prices, str):
                try:
                    prices = json.loads(raw_prices)
                    if isinstance(prices, list) and len(prices) >= 2:
                        yes_price = float(prices[0])
                        no_price = float(prices[1])
                except Exception:
                    pass
            elif isinstance(raw_prices, list) and len(raw_prices) >= 2:
                yes_price = float(raw_prices[0])
                no_price = float(raw_prices[1])

            # CLOB token ids (needed for order placement), aligned with outcomes order
            token_ids: list[str] = []
            raw_tokens = m.get("clobTokenIds")
            if isinstance(raw_tokens, str):
                try:
                    token_ids = [str(t) for t in json.loads(raw_tokens)]
                except Exception:
                    token_ids = []
            elif isinstance(raw_tokens, list):
                token_ids = [str(t) for t in raw_tokens]

            outcome_names: list[str] = []
            raw_outcomes = m.get("outcomes")
            if isinstance(raw_outcomes, str):
                try:
                    outcome_names = [str(o) for o in json.loads(raw_outcomes)]
                except Exception:
                    outcome_names = []
            elif isinstance(raw_outcomes, list):
                outcome_names = [str(o) for o in raw_outcomes]

            yes_token = no_token = ""
            if len(token_ids) == len(outcome_names) and outcome_names:
                for name, tid in zip(outcome_names, token_ids):
                    if name.upper() == "YES":
                        yes_token = tid
                    elif name.upper() == "NO":
                        no_token = tid
            if not yes_token and token_ids:
                yes_token = token_ids[0]
            if not no_token and len(token_ids) > 1:
                no_token = token_ids[1]

            # Fall back to embedded token list for prices if outcomePrices missing
            if yes_price == 0.5 and no_price == 0.5:
                for t in m.get("tokens", []):
                    if t.get("outcome", "").upper() == "YES":
                        yes_price = _f(t.get("price"), 0.5)
                    elif t.get("outcome", "").upper() == "NO":
                        no_price = _f(t.get("price"), 0.5)

            best_bid = m.get("bestBid")
            best_ask = m.get("bestAsk")

            description = (m.get("description") or "").strip()
            events = m.get("events") or []
            event_title = events[0].get("title", "") if events and isinstance(events[0], dict) else ""

            result.append({
                "market_id": m.get("conditionId", m.get("id", "")),
                "question": m.get("question", ""),
                "description": description,
                "event_title": event_title,
                "yes_price": yes_price,
                "no_price": no_price,
                "yes_token_id": yes_token,
                "no_token_id": no_token,
                "volume_24h": vol,
                "end_date": m.get("endDate", ""),
                "liquidity": _f(m.get("liquidityNum", m.get("liquidity"))),
                "spread": _f(m.get("spread"), 0.01),
                "best_bid": _f(best_bid, yes_price),
                "best_ask": _f(best_ask, yes_price),
                "neg_risk": bool(m.get("negRisk", False)),
                "accepting_orders": bool(m.get("acceptingOrders", True)),
            })
        return result

    async def resolve_token_id(self, market_id: str, side: str) -> str:
        """Look up the CLOB token id for a market condition + outcome side (live orders)."""
        side = side.upper()
        data = await self._get_json(f"{GAMMA_API}/markets", params={"condition_ids": market_id})
        markets = data if isinstance(data, list) else _extract(data) or []
        for m in markets:
            raw_tokens = m.get("clobTokenIds")
            tokens = []
            if isinstance(raw_tokens, str):
                try:
                    tokens = json.loads(raw_tokens)
                except Exception:
                    tokens = []
            elif isinstance(raw_tokens, list):
                tokens = raw_tokens
            raw_outcomes = m.get("outcomes")
            names = []
            if isinstance(raw_outcomes, str):
                try:
                    names = json.loads(raw_outcomes)
                except Exception:
                    names = []
            elif isinstance(raw_outcomes, list):
                names = raw_outcomes
            for name, tid in zip(names, tokens):
                if str(name).upper() == side:
                    return str(tid)
        raise RuntimeError(f"Could not resolve {side} token id for market {market_id[:16]}…")

    async def get_token_price(self, token_id: str, side: str = "buy") -> Optional[float]:
        """Executable price for a token from the public CLOB endpoint (best-effort)."""
        try:
            data = await self._get_json(f"{CLOB_API}/price", {"token_id": token_id, "side": side.lower()})
            price = data.get("price")
            return float(price) if price is not None else None
        except Exception as exc:
            logger.debug(f"get_token_price failed for {token_id[:12]}…: {exc}")
            return None

    async def get_price_change(self, token_id: str, interval: str = "1d") -> Optional[float]:
        """YES-token price change over the lookback window (corroboration momentum).

        Uses the public CLOB prices-history route. Returns a fraction
        (e.g. +0.04 = 4¢→ 4¢ up over the window), or None when history is thin.
        """
        if not token_id:
            return None
        try:
            data = await self._get_json(f"{CLOB_API}/prices-history", {
                "market": token_id, "interval": interval, "fidelity": 60,
            })
        except Exception as exc:
            logger.debug(f"prices-history failed for {token_id[:12]}…: {exc}")
            return None
        history = (data or {}).get("history") or []
        if len(history) < 2:
            return None
        try:
            first = float(history[0]["p"])
            last = float(history[-1]["p"])
        except (KeyError, TypeError, ValueError):
            return None
        if first <= 0:
            return None
        return round(last - first, 4)  # absolute probability shift (0-1 scale)

    async def get_tick_size(self, market_id: str) -> float:
        """Minimum price increment for a market (Gamma), defaulting to 0.01."""
        try:
            data = await self._get_json(f"{GAMMA_API}/markets", params={"condition_ids": market_id})
            markets = data if isinstance(data, list) else _extract(data) or []
            if markets:
                return float(markets[0].get("orderPriceMinTickSize") or 0.01)
        except Exception:
            pass
        return 0.01

    # ── My Profile ─────────────────────────────────────────────────────────────

    async def get_my_profile(self) -> dict | None:
        """Fetch profile stats via the Data API. Returns a stub if wallet has no history."""
        if not self.wallet_address:
            logger.warning("No wallet address available — set POLY_PRIVATE_KEY in .env")
            return None
        stats = await self.get_trader_stats(self.wallet_address)
        if stats is None:
            return {
                "address": self.wallet_address,
                "name": "",
                "roi": 0.0,
                "profit": 0.0,
                "volume": 0.0,
                "win_rate": 0.0,
                "positions_count": 0,
                "_no_history": True,
            }
        return stats

    async def get_my_trade_history(self, limit: int = 50) -> list[dict]:
        """Fetch your own recent trades via the Data API."""
        if not self.wallet_address:
            logger.warning("No wallet address available — set POLY_PRIVATE_KEY in .env")
            return []
        return await self.get_trader_recent_trades(self.wallet_address, limit=limit)

    async def get_my_open_positions(self) -> list[dict]:
        """Fetch your own open positions via the Data API."""
        if not self.wallet_address:
            logger.warning("No wallet address available — set POLY_PRIVATE_KEY in .env")
            return []
        return await self.get_trader_positions(self.wallet_address)

    # ── Leaderboard / Top Traders ──────────────────────────────────────────────

    async def get_top_traders(self, limit: int = 20, time_period: str = "ALL") -> list[dict]:
        """Fetch ranked traders from Data API v2 leaderboard.

        v2 only exposes pnl + volume per trader (no direct ROI/win-rate), so
        roi is computed as profit-per-dollar-traded and win_rate stays 0 until
        get_trader_stats can estimate it from closed positions.
        """
        params = {
            "limit": min(max(limit, 1), 50),
            "offset": 0,
            "order_by": "PNL",
            "time_period": time_period.lower(),
        }
        try:
            data = await self._get_json(f"{DATA_API}/v2/leaderboard", params)
        except Exception as exc:
            logger.error(f"Failed to fetch leaderboard: {exc}")
            return []

        traders = data if isinstance(data, list) else _extract(data) or []
        result = []
        for t in traders:
            address = t.get("user_id") or t.get("user") or t.get("proxy_wallet") or t.get("proxyWallet", "")
            pnl = _f(t.get("pnl"))
            volume = _f(t.get("volume", t.get("vol")))
            # roi proxy: net profit per dollar of lifetime volume traded
            roi = (pnl / volume) if volume > 0 else 0.0
            result.append({
                "address": address,
                "name": t.get("user_name") or t.get("userName", ""),
                "roi": roi,
                "profit": pnl,
                "volume": volume,
                "win_rate": _f(t.get("win_rate", t.get("winRate"))),
                "positions_count": int(_f(t.get("positions_count", t.get("positionsCount")))),
                "rank": int(_f(t.get("rank"), 0)),
            })
        return result

    async def get_trader_stats(self, address: str) -> dict | None:
        """Fetch per-trader stats from Data API v2 user-stats + leaderboard.

        Replaces the retired `GET /users/{address}` route (now 404s).
        """
        stats: dict | None = None
        try:
            data = await self._get_json(f"{DATA_API}/v2/user-stats", {"user": address})
            stats = _extract(data)
        except Exception as exc:
            logger.warning(f"v2 user-stats failed for {address[:12]}…: {exc}")
        if isinstance(stats, list):
            stats = stats[0] if stats else None
        if not isinstance(stats, dict):
            stats = None

        # Leaderboard row gives lifetime pnl + volume for this specific user
        lb: dict | None = None
        try:
            data = await self._get_json(f"{DATA_API}/v2/leaderboard", {"user": address, "limit": 1})
            rows = data if isinstance(data, list) else _extract(data) or []
            lb = rows[0] if rows else None
        except Exception as exc:
            logger.debug(f"leaderboard-by-user failed for {address[:12]}…: {exc}")

        if stats is None and lb is None:
            return None

        pnl = _f(lb.get("pnl")) if lb else 0.0
        volume = _f(lb.get("volume", lb.get("vol"))) if lb else 0.0
        if not pnl and stats:
            atp = stats.get("all_time_pnl") or {}
            if isinstance(atp, dict):
                pnl = _f(atp.get("realized_market_pnl")) + _f(atp.get("realized_lp_pnl")) + _f(atp.get("realized_combo_pnl"))
        profit = pnl
        roi = (pnl / volume) if volume > 0 else 0.0
        trades_count = int(_f((stats or {}).get("trades")))

        return {
            "address": address,
            "name": (lb or {}).get("user_name") or (lb or {}).get("userName") or "",
            "roi": roi,
            "profit": profit,
            "volume": volume,
            "win_rate": 0.0,  # not exposed by the API — see get_trader_win_rate
            "positions_count": trades_count,
            "biggest_win": _f((stats or {}).get("biggest_win")),
            "join_date": (stats or {}).get("join_date"),
        }

    async def get_trader_win_rate(self, address: str, sample: int = 100) -> float | None:
        """Estimate hit-rate from recently closed positions (percent_realized_pnl > 0)."""
        try:
            data = await self._get_json(
                f"{DATA_API}/v2/positions",
                {"user": address, "status": "CLOSED", "limit": min(sample, 500)},
            )
            rows = data if isinstance(data, list) else _extract(data) or []
        except Exception as exc:
            logger.debug(f"win-rate estimate failed for {address[:12]}…: {exc}")
            return None
        if not rows:
            return None
        wins = sum(1 for p in rows if _f(p.get("percent_realized_pnl", p.get("realized_pnl"))) > 0)
        return round(wins / len(rows) * 100.0, 1)

    async def get_trader_positions(self, address: str) -> list[dict]:
        """Open positions for a trader via Data API v2 lifecycle route.

        v2 `status=OPEN` already excludes closed/redeemed positions, so the old
        v1 heuristics (missing redeemed/closed flags) are no longer needed.
        """
        params = {"user": address, "status": "OPEN", "limit": 500}
        try:
            data = await self._get_json(f"{DATA_API}/v2/positions", params)
        except Exception as exc:
            logger.warning(f"get_trader_positions failed for {address[:12]}…: {exc}")
            return []

        rows = data if isinstance(data, list) else _extract(data) or []
        now = datetime.now(timezone.utc)
        result = []
        for p in rows:
            condition_id = p.get("condition_id") or p.get("conditionId", "")
            if not condition_id:
                continue
            current_price = _f(p.get("current_price", p.get("curPrice")), 0.5)
            # Skip settled rows that still show as open (price pinned at 0/1)
            if current_price == 0.0 or current_price == 1.0:
                continue
            end_date_raw = p.get("end_date") or p.get("endDate", "")
            if end_date_raw:
                try:
                    end_dt = datetime.fromisoformat(str(end_date_raw).replace("Z", "+00:00"))
                    if end_dt < now:
                        continue
                except Exception:
                    pass
            shares = _f(p.get("current_size", p.get("size")))
            entry_cost = _f(p.get("entry_cost_usdc", p.get("initialValue")))
            avg_price = _f(p.get("avg_price")) if "avg_price" in p else 0.0
            if avg_price <= 0 and shares > 0 and entry_cost > 0:
                avg_price = entry_cost / shares
            if avg_price <= 0 or avg_price > 1:
                avg_price = 0.5
            size_usd = entry_cost if entry_cost > 0 else _f(p.get("current_value")) or shares * avg_price

            result.append({
                "market_id": condition_id,
                "token_id": p.get("token_id", ""),
                "question": p.get("title") or p.get("name", ""),
                "side": (p.get("outcome") or "").upper(),
                "size": size_usd,
                "shares": shares,
                "avg_price": avg_price,
                "current_price": current_price,
                "first_entry_at": p.get("first_entry_at"),
                "last_event_at": p.get("last_event_at"),
            })
        return result

    async def get_trader_recent_trades(self, address: str, limit: int = 20) -> list[dict]:
        """Fetch recent TRADE activity by a trader (v2 activity route).

        Filters to type=TRADE so REDEEM/MERGE/REWARD rows don't masquerade as
        trades (v1 callers had no type filter and polluted charts).
        """
        params = {"user": address, "limit": min(max(limit, 1), 100), "type": "TRADE"}
        try:
            data = await self._get_json(f"{DATA_API}/v2/activity", params)
        except Exception as exc:
            logger.warning(f"get_trader_recent_trades failed for {address[:12]}…: {exc}")
            return []

        rows = data if isinstance(data, list) else _extract(data) or []
        result = []
        for t in rows:
            if str(t.get("type", "TRADE")).upper() != "TRADE":
                continue
            outcome = str(t.get("outcome", "")).upper()
            action = str(t.get("side", "")).upper()  # BUY | SELL
            size_usd = _f(t.get("usdc_size", t.get("usdcSize")))
            if size_usd <= 0:
                continue
            result.append({
                "market_id": t.get("condition_id") or t.get("conditionId", ""),
                "token_id": t.get("token_id") or t.get("asset", ""),
                "question": t.get("title", ""),
                "side": outcome or ("YES" if action == "BUY" else "NO"),
                "action": action,
                "size": size_usd,
                "shares": _f(t.get("size")),
                "price": _f(t.get("price"), 0.5),
                "timestamp": t.get("timestamp", ""),
            })
        return result

    # ── CLOB Order Execution (live mode) ──────────────────────────────────────

    async def _get_sdk_client(self):
        """Create (once) the official Polymarket secure client for signed orders."""
        if self._sdk_client is not None:
            return self._sdk_client
        if not self.private_key:
            raise RuntimeError("POLY_PRIVATE_KEY is required for live trading")
        try:
            from polymarket import AsyncSecureClient
        except ImportError as exc:
            raise RuntimeError(
                "Live trading requires the official SDK: pip install polymarket-client"
            ) from exc
        kwargs: dict[str, Any] = {"private_key": self.private_key}
        if self.wallet_address:
            kwargs["wallet"] = self.wallet_address
        self._sdk_client = await AsyncSecureClient.create(**kwargs)
        return self._sdk_client

    async def place_market_order(self, token_id: str, side: str, amount_usdc: float) -> dict[str, Any]:
        """Place a signed market order via the official polymarket-client SDK.

        token_id must be the CLOB token id for the outcome being bought
        (see resolve_token_id) — NOT the market condition id.
        BUY amount is USD to spend; SELL amount is shares to sell.
        """
        if not token_id:
            raise RuntimeError("Cannot place order: empty token id (live mode needs the CLOB token_id)")
        client = await self._get_sdk_client()
        response = await client.place_market_order(
            asset_id=str(token_id),
            side=side.upper(),
            amount=str(amount_usdc),
        )
        if not getattr(response, "ok", True):
            raise RuntimeError(f"Order rejected: {getattr(response, 'message', 'unknown error')}")
        return {
            "order_id": getattr(response, "order_id", ""),
            "status": "submitted",
        }

    async def place_limit_order(
        self, token_id: str, side: str, price: float, size_shares: float, post_only: bool = True,
    ) -> dict[str, Any]:
        """Rest a GTC limit order (earn the spread instead of crossing it).

        Price is snapped down to the 0.01 grid by the caller; the CLOB rejects
        non-conforming ticks, so keep this conservative.
        """
        if not token_id:
            raise RuntimeError("Cannot place limit order: empty CLOB token id")
        client = await self._get_sdk_client()
        response = await client.place_limit_order(
            asset_id=str(token_id),
            side=side.upper(),
            price=f"{price:.2f}",
            size=f"{size_shares:.2f}",
            post_only=post_only,
        )
        if not getattr(response, "ok", True):
            raise RuntimeError(f"Limit order rejected: {getattr(response, 'message', 'unknown error')}")
        return {
            "order_id": getattr(response, "order_id", ""),
            "status": "live",
        }

    async def get_my_positions(self) -> list[dict]:
        """Fetch authenticated user's positions from CLOB (L2 HMAC headers)."""
        path = "/positions"
        headers = self._clob_headers("GET", path)
        client = await self._get_http()
        resp = await client.get(f"{CLOB_API}{path}", headers=headers)
        if resp.status_code >= 400:
            logger.warning(f"get_my_positions failed: HTTP {resp.status_code} {resp.text}")
            return []
        return resp.json()

