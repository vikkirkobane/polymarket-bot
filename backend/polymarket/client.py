"""backend/polymarket/client.py — Async Polymarket API client."""

import base64
import hashlib
import hmac as hmac_lib
import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"


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
        """Build Polymarket CLOB L2 HMAC authentication headers."""
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
        """Fetch active binary markets from the Gamma API."""
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{GAMMA_API}/markets", params=params, headers=self._public_headers)
            resp.raise_for_status()
            data = resp.json()

        markets = data if isinstance(data, list) else data.get("data", [])
        result = []
        for m in markets:
            vol = float(m.get("volume24hr", 0) or 0)
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

            if yes_price == 0.5 and no_price == 0.5:
                tokens = m.get("tokens", [])
                for t in tokens:
                    if t.get("outcome", "").upper() == "YES":
                        yes_price = float(t.get("price", 0.5))
                    elif t.get("outcome", "").upper() == "NO":
                        no_price = float(t.get("price", 0.5))

            result.append({
                "market_id": m.get("conditionId", m.get("id", "")),
                "question": m.get("question", ""),
                "description": m.get("description", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "volume_24h": vol,
                "end_date": m.get("endDate", ""),
                "liquidity": float(m.get("liquidity", 0) or 0),
            })
        return result

    # ── My Profile ─────────────────────────────────────────────────────────────

    async def get_my_profile(self) -> dict | None:
        """Fetch profile stats via the public Data API. Returns a stub if wallet has no history."""
        if not self.wallet_address:
            logger.warning("No wallet address available — set POLY_PRIVATE_KEY in .env")
            return None
        stats = await self.get_trader_stats(self.wallet_address)
        if stats is None:
            # Wallet exists but has no Polymarket trading history yet
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
        """Fetch your own recent trades via the public Data API."""
        if not self.wallet_address:
            logger.warning("No wallet address available — set POLY_PRIVATE_KEY in .env")
            return []
        return await self.get_trader_recent_trades(self.wallet_address, limit=limit)

    async def get_my_open_positions(self) -> list[dict]:
        """Fetch your own open positions via the public Data API."""
        if not self.wallet_address:
            logger.warning("No wallet address available — set POLY_PRIVATE_KEY in .env")
            return []
        return await self.get_trader_positions(self.wallet_address)

    # ── Leaderboard / Top Traders ──────────────────────────────────────────────

    async def get_trader_stats(self, address: str) -> dict | None:
        """Fetch stats for a single trader by address. Tries checksum then lowercase."""
        candidates = [address, address.lower()]
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for addr in candidates:
                try:
                    resp = await client.get(f"{DATA_API}/users/{addr}", headers=self._public_headers)
                    if resp.status_code == 404:
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    return {
                        "address": address,
                        "name": data.get("userName", data.get("name", "")),
                        "roi": float(data.get("roi", 0) or 0),
                        "profit": float(data.get("pnl", data.get("profit", 0)) or 0),
                        "volume": float(data.get("vol", data.get("volume", 0)) or 0),
                        "win_rate": float(data.get("winRate", 0) or 0),
                        "positions_count": int(data.get("positionsCount", 0) or 0),
                    }
                except Exception as exc:
                    logger.warning(f"Failed to fetch stats for {addr}: {exc}")
        return None

    async def get_top_traders(self, limit: int = 20) -> list[dict]:
        """Fetch top traders by profit from the data API."""
        params = {
            "limit": limit,
            "offset": 0,
            "orderBy": "PNL",
            "timePeriod": "ALL",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(f"{DATA_API}/v1/leaderboard", params=params, headers=self._public_headers)
                if resp.status_code >= 400:
                    logger.warning(f"Leaderboard API error: {resp.status_code} {resp.text}")
                    return []
                data = resp.json()
            except Exception as exc:
                logger.error(f"Failed to fetch leaderboard: {exc}")
                return []

        traders = data if isinstance(data, list) else data.get("data", [])
        result = []
        for t in traders:
            result.append({
                "address": t.get("proxyWallet", t.get("address", "")),
                "name": t.get("userName", t.get("name", "")),
                "roi": float(t.get("roi", 0) or 0),
                "profit": float(t.get("pnl", t.get("profit", 0)) or 0),
                "volume": float(t.get("vol", t.get("volume", 0)) or 0),
                "win_rate": float(t.get("winRate", 0) or 0),
                "positions_count": int(t.get("positionsCount", 0) or 0),
            })
        return result

    async def get_trader_positions(self, address: str) -> list[dict]:
        """Get open positions for a specific trader wallet. Skips resolved/redeemed markets."""
        from datetime import datetime, timezone
        params = {"user": address}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{DATA_API}/v1/positions", params=params, headers=self._public_headers)
            if resp.status_code >= 400:
                logger.warning(f"get_trader_positions failed for {address}: HTTP {resp.status_code}")
                return []
            data = resp.json()

        positions = data if isinstance(data, list) else data.get("data", [])
        now = datetime.now(timezone.utc)
        result = []
        for p in positions:
            # Skip already-redeemed positions
            if p.get("redeemed") or p.get("redeemedAt"):
                continue
            # Skip explicitly closed markets
            if p.get("closed") or p.get("closedAt"):
                continue
            # Skip markets whose end date has passed
            end_date_raw = p.get("endDate") or p.get("end_date_iso") or p.get("endDateIso", "")
            if end_date_raw:
                try:
                    end_dt = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
                    if end_dt < now:
                        continue
                except Exception:
                    pass
            raw_cur = p.get("curPrice", p.get("currentPrice"))
            cur_price = float(raw_cur) if raw_cur is not None else 0.5
            # Skip markets that have already fully resolved (price pinned to 0 or 1)
            if cur_price == 0.0 or cur_price == 1.0:
                continue
            result.append({
                "market_id": p.get("conditionId", ""),
                "question": p.get("title", p.get("question", "")),
                "side": p.get("outcome", "").upper(),
                "size": float(p.get("initialValue", p.get("size", 0)) or 0),
                "avg_price": float(p.get("avgPrice", 0.5) or 0.5),
                "current_price": cur_price,
            })
        return result

    async def get_trader_recent_trades(self, address: str, limit: int = 20) -> list[dict]:
        """Fetch recent trades made by a trader."""
        params = {"user": address, "limit": limit}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{DATA_API}/v1/activity", params=params, headers=self._public_headers)
            if resp.status_code >= 400:
                logger.warning(f"get_trader_recent_trades failed for {address}: HTTP {resp.status_code}")
                return []
            data = resp.json()

        trades = data if isinstance(data, list) else data.get("data", [])
        result = []
        for t in trades:
            # outcome (YES/NO) is the market side; side (BUY/SELL) is the action
            outcome = t.get("outcome", "").upper()
            action  = t.get("side", "").upper()
            # Selling YES == bearish, buying NO == bearish; normalise to a single label
            if outcome in ("YES", "NO"):
                market_side = outcome
            elif action == "BUY":
                market_side = "YES"
            elif action == "SELL":
                market_side = "NO"
            else:
                market_side = action or "?"
            size = float(t.get("usdcSize", t.get("size", 0)) or 0)
            if size <= 0:
                continue  # skip settlements / cancels
            result.append({
                "market_id": t.get("conditionId", ""),
                "question": t.get("title", ""),
                "side": market_side,
                "action": action,
                "size": size,
                "price": float(t.get("price", 0.5) or 0.5),
                "timestamp": t.get("timestamp", ""),
            })
        return result

    # ── CLOB Order Execution ───────────────────────────────────────────────────

    async def place_market_order(self, token_id: str, side: str, amount_usdc: float) -> dict[str, Any]:
        """Place a market order on the CLOB using HMAC-signed headers."""
        path = "/order"
        payload = {
            "tokenID": token_id,
            "side": side.upper(),
            "amount": amount_usdc,
            "type": "MARKET",
        }
        body = json.dumps(payload)
        headers = self._clob_headers("POST", path, body)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{CLOB_API}{path}", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def get_my_positions(self) -> list[dict]:
        """Fetch authenticated user's positions from CLOB."""
        path = "/positions"
        headers = self._clob_headers("GET", path)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{CLOB_API}{path}", headers=headers)
            if resp.status_code >= 400:
                logger.warning(f"get_my_positions failed: HTTP {resp.status_code} {resp.text}")
                return []
            return resp.json()
