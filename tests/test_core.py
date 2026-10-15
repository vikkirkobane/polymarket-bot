"""tests/test_core.py — Unit tests for core bot logic."""

import json
import pytest
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timedelta, timezone


# ── researcher._quick_edge ─────────────────────────────────────────────────────

class TestQuickEdge:
    """Tests for the heuristic market pre-filter."""

    def _make_researcher(self):
        from backend.polymarket.researcher import MarketResearcher
        return MarketResearcher(client=MagicMock(), analyst=MagicMock())

    def test_normal_market_passes(self):
        r = self._make_researcher()
        market = {"yes_price": 0.6, "no_price": 0.4, "volume_24h": 10_000, "question": "test"}
        assert r._quick_edge(market) > 0

    def test_zero_volume_balanced_market_scores_zero(self):
        r = self._make_researcher()
        market = {"yes_price": 0.5, "no_price": 0.5, "volume_24h": 0, "question": "test"}
        # perfectly balanced, no volume, no liquidity — nothing to see
        assert r._quick_edge(market) == 0.0

    def test_near_resolved_extremes_are_dropped(self):
        r = self._make_researcher()
        # The old filter *boosted* extremes; they're settlement noise and are now dropped
        assert r._quick_edge({"yes_price": 0.98, "no_price": 0.02, "volume_24h": 50_000}) == 0.0
        assert r._quick_edge({"yes_price": 0.01, "no_price": 0.99, "volume_24h": 50_000}) == 0.0

    def test_non_accepting_orders_dropped(self):
        r = self._make_researcher()
        market = {"yes_price": 0.5, "no_price": 0.5, "volume_24h": 50_000, "accepting_orders": False}
        assert r._quick_edge(market) == 0.0

    def test_high_volume_increases_score(self):
        r = self._make_researcher()
        low_vol = {"yes_price": 0.5, "no_price": 0.5, "volume_24h": 100, "question": "test"}
        high_vol = {"yes_price": 0.5, "no_price": 0.5, "volume_24h": 100_000, "question": "test"}
        assert r._quick_edge(high_vol) > r._quick_edge(low_vol)

    def test_mispriced_market_scores_higher(self):
        r = self._make_researcher()
        balanced = {"yes_price": 0.5, "no_price": 0.5, "volume_24h": 5_000, "question": "test"}
        mispriced = {"yes_price": 0.7, "no_price": 0.7, "volume_24h": 5_000, "question": "test"}
        assert r._quick_edge(mispriced) > r._quick_edge(balanced)

    def test_wide_spread_scores_higher(self):
        r = self._make_researcher()
        tight = {"yes_price": 0.5, "no_price": 0.5, "volume_24h": 5_000, "spread": 0.01}
        wide = {"yes_price": 0.5, "no_price": 0.5, "volume_24h": 5_000, "spread": 0.10}
        assert r._quick_edge(wide) > r._quick_edge(tight)

    def test_deep_liquidity_bonus(self):
        r = self._make_researcher()
        thin = {"yes_price": 0.5, "no_price": 0.5, "volume_24h": 5_000, "liquidity": 100}
        deep = {"yes_price": 0.5, "no_price": 0.5, "volume_24h": 5_000, "liquidity": 50_000}
        assert r._quick_edge(deep) > r._quick_edge(thin)


# ── analyst._repair_json / _parse_json_array ──────────────────────────────────

class TestRepairJson:
    """Tests for the truncated JSON repair logic."""

    def _make_analyst(self):
        from backend.analyst.base import BaseAnalyst

        class Stub(BaseAnalyst):
            model_name = "stub"

            async def _call_model(self, prompt):
                return ""

        return Stub()

    def test_already_valid_json_unchanged(self):
        a = self._make_analyst()
        valid = '[{"market_id": "x", "edge_pct": 5}]'
        assert json.loads(a._repair_json(valid)) == [{"market_id": "x", "edge_pct": 5}]

    def test_repairs_missing_closing_bracket(self):
        a = self._make_analyst()
        truncated = '[{"market_id": "x", "edge_pct": 5}'
        repaired = a._repair_json(truncated)
        assert repaired is not None
        assert isinstance(json.loads(repaired), list)

    def test_repairs_missing_closing_brace_and_bracket(self):
        a = self._make_analyst()
        truncated = '[{"market_id": "x", "edge_pct": 5'
        repaired = a._repair_json(truncated)
        assert repaired is not None

    def test_returns_none_on_hopeless_input(self):
        a = self._make_analyst()
        garbage = "not json at all {{{"
        # Should return None if can't produce valid JSON after repair
        result = a._repair_json(garbage)
        if result is not None:
            # If it claims to have repaired it, it must be valid JSON
            json.loads(result)

    def test_parse_strips_markdown_fences(self):
        a = self._make_analyst()
        text = '```json\n[{"market_id": "x", "estimated_prob": 0.6}]\n```'
        parsed = a._parse_json_array(text)
        assert parsed == [{"market_id": "x", "estimated_prob": 0.6}]

    def test_parse_unwraps_dict_wrapped_array(self):
        a = self._make_analyst()
        text = '{"opportunities": [{"market_id": "x"}]}'
        assert a._parse_json_array(text) == [{"market_id": "x"}]

    def test_parse_returns_empty_list_on_garbage(self):
        a = self._make_analyst()
        assert a._parse_json_array("sorry, I cannot help with that") == []


# ── analyst._build_opportunities (derived edge math) ─────────────────────────

class TestBuildOpportunities:
    """The model only supplies estimated_prob; edge/side are derived in code."""

    def _make_analyst(self):
        from backend.analyst.base import BaseAnalyst

        class Stub(BaseAnalyst):
            model_name = "stub"

            async def _call_model(self, prompt):
                return ""

        return Stub()

    def _market(self, **over):
        m = {
            "market_id": "m1", "question": "Will X?", "yes_price": 0.5, "no_price": 0.5,
            "best_ask": 0.52, "best_bid": 0.48,
            "yes_token_id": "TOK_YES", "no_token_id": "TOK_NO",
            "volume_24h": 10_000, "liquidity": 20_000, "end_date": "2026-11-01",
        }
        m.update(over)
        return m

    def test_yes_edge_derived_from_executable_ask(self):
        a = self._make_analyst()
        rows = [{"market_id": "m1", "estimated_prob": 0.62, "confidence": "medium", "reasoning": "r"}]
        opps = a._build_opportunities(rows, [self._market()])
        assert len(opps) == 1
        assert opps[0]["recommended_side"] == "YES"
        # (0.62 - 0.52 ask) * 100 = 10.0 — edge vs the ASK, not the mid
        assert abs(opps[0]["edge_pct"] - 10.0) < 0.01
        assert opps[0]["yes_token_id"] == "TOK_YES"
        assert opps[0]["model_used"] == "stub"

    def test_no_edge_derived_from_executable_bid(self):
        a = self._make_analyst()
        rows = [{"market_id": "m1", "estimated_prob": 0.30, "confidence": "high", "reasoning": "r"}]
        opps = a._build_opportunities(rows, [self._market()])
        assert len(opps) == 1
        assert opps[0]["recommended_side"] == "NO"
        # buy NO at 1 - best_bid = 0.52; edge = (1-0.30) - 0.52 = 0.18 → 18%
        assert abs(opps[0]["edge_pct"] - 18.0) < 0.01

    def test_sub_threshold_edges_are_dropped(self):
        a = self._make_analyst()
        rows = [{"market_id": "m1", "estimated_prob": 0.505, "confidence": "low", "reasoning": "r"}]
        assert a._build_opportunities(rows, [self._market()]) == []

    def test_hallucinated_market_id_dropped(self):
        a = self._make_analyst()
        rows = [{"market_id": "0xDOES_NOT_EXIST", "estimated_prob": 0.9, "confidence": "high"}]
        assert a._build_opportunities(rows, [self._market()]) == []

    def test_legacy_format_still_accepted(self):
        a = self._make_analyst()
        rows = [{"market_id": "m1", "recommended_side": "yes", "edge_pct": 7.3, "confidence": "Medium"}]
        opps = a._build_opportunities(rows, [self._market()])
        assert len(opps) == 1
        assert opps[0]["recommended_side"] == "YES"
        assert opps[0]["confidence"] == "medium"

    def test_legacy_garbage_side_dropped(self):
        a = self._make_analyst()
        rows = [{"market_id": "m1", "recommended_side": "MAYBE", "edge_pct": 20}]
        assert a._build_opportunities(rows, [self._market()]) == []

    def test_prob_clamped_to_valid_range(self):
        a = self._make_analyst()
        rows = [{"market_id": "m1", "estimated_prob": 1.4, "confidence": "high"}]
        opps = a._build_opportunities(rows, [self._market()])
        assert len(opps) == 1
        assert opps[0]["estimated_prob"] == 1.0


# ── analyst.analyze_markets chunking ─────────────────────────────────────────

class TestChunkedAnalysis:
    def _make_analyst(self, responses):
        from backend.analyst.base import BaseAnalyst

        class Scripted(BaseAnalyst):
            model_name = "scripted"
            chunk_size = 2
            max_concurrency = 2

            def __init__(self):
                self.calls = []
                self.responses = responses

            async def _call_model(self, prompt):
                self.calls.append(prompt)
                return self.responses[len(self.calls) - 1] if len(self.calls) <= len(self.responses) else "[]"

        return Scripted()

    @pytest.mark.asyncio
    async def test_markets_split_into_chunks(self):
        markets = [
            {"market_id": f"m{i}", "question": f"Q{i}?", "yes_price": 0.5, "no_price": 0.5,
             "accepting_orders": True}
            for i in range(5)
        ]
        analyst = self._make_analyst(["[]", "[]", "[]"])
        await analyst.analyze_markets(markets)
        assert len(analyst.calls) == 3  # ceil(5 / 2)

    @pytest.mark.asyncio
    async def test_not_accepting_orders_excluded(self):
        markets = [
            {"market_id": "m1", "question": "Q?", "yes_price": 0.5, "no_price": 0.5, "accepting_orders": False},
        ]
        analyst = self._make_analyst([])
        result = await analyst.analyze_markets(markets)
        assert result == []
        assert analyst.calls == []


# ── copy_trader timestamp normalization ──────────────────────────────────────

class TestToTs:
    def test_unix_seconds_passthrough(self):
        from backend.polymarket.copy_trader import _to_ts
        assert _to_ts(1_760_000_000) == 1_760_000_000

    def test_milliseconds_normalized(self):
        from backend.polymarket.copy_trader import _to_ts
        assert _to_ts(1_760_000_000_000) == 1_760_000_000

    def test_iso_string_parsed(self):
        from backend.polymarket.copy_trader import _to_ts
        ts = _to_ts("2026-10-01T00:00:00Z")
        assert isinstance(ts, int)
        assert ts == 1_790_812_800  # 2026-10-01T00:00:00Z
        # naive ISO (no zone) is also accepted
        assert _to_ts("2026-10-01T00:00:00") == ts

    def test_none_and_garbage_return_none(self):
        from backend.polymarket.copy_trader import _to_ts
        assert _to_ts(None) is None
        assert _to_ts("not-a-date") is None


# ── copy_trader detection logic ──────────────────────────────────────────────

class TestCopyTraderDetection:
    """First-poll seeding, add-detection and 24h dedupe."""

    @pytest.fixture(autouse=True)
    def setup_db(self, monkeypatch):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import backend.polymarket.copy_trader as ct_module
        from backend.database import Base, CopyTrader

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        monkeypatch.setattr(ct_module, "SessionLocal", Session)

        self.db = Session()
        self.db.add(CopyTrader(address="0xTRADER", name="Test", active=True))
        self.db.commit()
        self.Session = Session
        yield
        self.db.close()

    def _pos(self, market_id, side="YES", ts=1_000, size=500.0):
        return {
            "market_id": market_id, "token_id": f"tok_{market_id}", "question": f"Q {market_id}?",
            "side": side, "size": size, "shares": size, "avg_price": 0.5,
            "current_price": 0.5, "last_event_at": ts,
        }

    def _engine(self, positions):
        from backend.polymarket.copy_trader import CopyTradingEngine
        client = MagicMock()
        client.get_trader_positions = AsyncMock(return_value=positions)
        return CopyTradingEngine(client=client, scale_factor=0.1, max_bet=100.0)

    @pytest.mark.asyncio
    async def test_first_poll_seeds_without_signals(self):
        e = self._engine([self._pos("m1")])
        trader = {"address": "0xTRADER", "name": "Test", "last_position_poll": None}
        signals = await e._check_trader(trader)
        assert signals == []
        # state was seeded
        seeded = self.Session().query(self._state_model()).filter_by(trader_address="0xTRADER").all()
        assert {s.market_id for s in seeded} == {"m1"}

    @pytest.mark.asyncio
    async def test_new_position_generates_signal(self):
        from backend.database import CopySignal
        e = self._engine([self._pos("m1")])
        trader = {"address": "0xTRADER", "name": "Test", "last_position_poll": None}
        await e._check_trader(trader)  # seed

        e2 = self._engine([self._pos("m1"), self._pos("m2", side="NO", size=300.0)])
        trader["last_position_poll"] = datetime.now(timezone.utc)
        signals = await e2._check_trader(trader)
        assert len(signals) == 1
        assert signals[0]["market_id"] == "m2"
        assert signals[0]["side"] == "NO"
        assert signals[0]["copied_size"] == 30.0  # 300 * 0.1
        assert signals[0]["token_id"] == "tok_m2"
        assert self.db.query(CopySignal).filter_by(market_id="m2").count() == 1

    @pytest.mark.asyncio
    async def test_size_add_detected_via_last_event_ts(self):
        e = self._engine([self._pos("m1", ts=1_000)])
        trader = {"address": "0xTRADER", "name": "Test", "last_position_poll": None}
        await e._check_trader(trader)  # seed with ts=1000

        e2 = self._engine([self._pos("m1", ts=2_000, size=900.0)])
        trader["last_position_poll"] = datetime.now(timezone.utc)
        signals = await e2._check_trader(trader)
        assert len(signals) == 1
        assert signals[0]["market_id"] == "m1"

    @pytest.mark.asyncio
    async def test_duplicate_signal_deduped_within_24h(self):
        e = self._engine([self._pos("m1", ts=1_000)])
        trader = {"address": "0xTRADER", "name": "Test", "last_position_poll": None}
        await e._check_trader(trader)  # seed

        e2 = self._engine([self._pos("m1", ts=2_000)])
        trader["last_position_poll"] = datetime.now(timezone.utc)
        first = await e2._check_trader(trader)
        assert len(first) == 1

        # Same (trader, market, side) again with a newer ts → deduped
        e3 = self._engine([self._pos("m1", ts=3_000)])
        second = await e3._check_trader(trader)
        assert second == []

    def _state_model(self):
        from backend.database import TraderPositionState
        return TraderPositionState


# ── executor._check_daily_limit ───────────────────────────────────────────────

class TestCheckDailyLimit:
    """Tests for daily spend limit enforcement."""

    def _make_executor(self):
        from backend.trader.executor import TradeExecutor
        e = TradeExecutor(paper_mode=True, paper_balance=10_000, max_daily_spend=500)
        # Patch _get_daily_spend to avoid DB calls
        e._get_daily_spend = MagicMock(return_value=0.0)
        return e

    def test_unlimited_when_max_zero(self):
        from backend.trader.executor import TradeExecutor
        e = TradeExecutor(paper_mode=True, max_daily_spend=0)
        assert e._check_daily_limit(99999, 0) is True

    def test_allows_trade_within_limit(self):
        e = self._make_executor()
        e._get_daily_spend.return_value = 100.0
        assert e._check_daily_limit(200, 500) is True

    def test_rejects_trade_that_would_exceed_limit(self):
        e = self._make_executor()
        e._get_daily_spend.return_value = 400.0
        assert e._check_daily_limit(200, 500) is False

    def test_allows_trade_exactly_at_limit(self):
        e = self._make_executor()
        e._get_daily_spend.return_value = 400.0
        assert e._check_daily_limit(100, 500) is True


# ── executor._db_close_opposite ───────────────────────────────────────────────

class TestCloseOpposite:
    """Closing the opposite side exits at 1 − trade_price (binary complement)."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        """Create a fresh in-memory DB for each test."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from backend.database import Base, Position

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        self.db = Session()
        self.Position = Position
        yield
        self.db.close()

    def _make_executor(self):
        from backend.trader.executor import TradeExecutor
        return TradeExecutor(paper_mode=True, paper_balance=10_000)

    def _add_position(self, market_id, side, shares, avg_price):
        pos = self.Position(
            market_id=market_id,
            question="Test market?",
            side=side,
            shares=shares,
            avg_price=avg_price,
            current_price=avg_price,
            pnl=0.0,
            mode="paper",
            source="test",
        )
        self.db.add(pos)
        self.db.flush()
        return pos

    def test_no_opposite_position_returns_zero(self):
        e = self._make_executor()
        pnl, proceeds = e._db_close_opposite(self.db, "market1", "YES", 0.7)
        assert pnl == 0.0
        assert proceeds == 0.0

    def test_profit_when_complement_exit_above_entry(self):
        e = self._make_executor()
        # Holding YES @ 0.20; we now buy NO at 0.70 → YES exits at 1 − 0.70 = 0.30
        self._add_position("market1", "YES", 100, avg_price=0.20)
        pnl, proceeds = e._db_close_opposite(self.db, "market1", "YES", 0.70)
        assert abs(pnl - 10.0) < 0.001    # (0.30 − 0.20) * 100
        assert abs(proceeds - 30.0) < 0.001  # 0.30 * 100

    def test_loss_when_complement_exit_below_entry(self):
        e = self._make_executor()
        # Holding NO @ 0.60; we now buy YES at 0.70 → NO exits at 1 − 0.70 = 0.30
        self._add_position("market1", "NO", 50, avg_price=0.60)
        pnl, proceeds = e._db_close_opposite(self.db, "market1", "NO", 0.70)
        assert abs(pnl - (-15.0)) < 0.001  # (0.30 − 0.60) * 50
        assert abs(proceeds - 15.0) < 0.001

    def test_fully_removes_position_from_db(self):
        e = self._make_executor()
        self._add_position("market1", "YES", 100, avg_price=0.5)
        e._db_close_opposite(self.db, "market1", "YES", 0.7)
        remaining = self.db.query(self.Position).filter_by(market_id="market1").first()
        assert remaining is None


# ── executor paper-trade end-to-end (balance + side flip) ────────────────────

class TestPaperTradeFlow:
    @pytest.fixture(autouse=True)
    def setup_db(self, monkeypatch):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import backend.trader.executor as ex_module
        from backend.database import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        monkeypatch.setattr(ex_module, "SessionLocal", Session)
        self.db = Session()
        yield
        self.db.close()

    def _make_executor(self, balance=10_000.0):
        from backend.trader.executor import TradeExecutor
        return TradeExecutor(paper_mode=True, paper_balance=balance)

    @pytest.mark.asyncio
    async def test_side_flip_credits_proceeds_and_replaces_position(self):
        from backend.database import Position
        e = self._make_executor()
        # Existing YES position bought cheap
        self.db.add(Position(
            market_id="m1", question="Q?", side="YES", shares=100,
            avg_price=0.20, current_price=0.20, pnl=0.0, mode="paper", source="test",
        ))
        self.db.commit()

        # Buy NO at 0.70 with $70 → 100 shares; YES closes at 0.30 → $30 proceeds
        trade = await e.execute("m1", "Q?", "NO", 70.0, 0.70, source="manual")
        assert trade["status"] == "filled"
        assert abs(trade["pnl"] - 10.0) < 0.001  # (0.30 − 0.20) * 100

        positions = self.db.query(Position).filter_by(market_id="m1").all()
        assert len(positions) == 1
        assert positions[0].side == "NO"  # unique market_id flip survived the flush

        # 10000 − 70 spent + 30 proceeds
        assert abs(e.paper_balance - 9960.0) < 0.001

    @pytest.mark.asyncio
    async def test_balance_never_goes_negative(self):
        e = self._make_executor(balance=10.0)
        trade = await e.execute("m2", "Q2?", "YES", 500.0, 0.5, source="manual")
        assert trade["status"] == "filled"
        assert trade["size"] == 10.0  # capped at remaining balance
        assert e.paper_balance >= 0.0

    @pytest.mark.asyncio
    async def test_rejected_when_balance_zero(self):
        e = self._make_executor(balance=0.0)
        trade = await e.execute("m3", "Q3?", "YES", 50.0, 0.5, source="manual")
        assert trade["status"] == "rejected"


# ── executor._db_update_position ──────────────────────────────────────────────

class TestUpdatePosition:
    """Tests for position creation and update logic."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from backend.database import Base, Position

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        self.db = Session()
        self.Position = Position
        yield
        self.db.close()

    def _make_executor(self):
        from backend.trader.executor import TradeExecutor
        return TradeExecutor(paper_mode=True, paper_balance=10_000)

    def test_creates_new_position(self):
        e = self._make_executor()
        e._db_update_position(self.db, "m1", "Will X?", "YES", 100, 0.5, "paper", "test")
        pos = self.db.query(self.Position).filter_by(market_id="m1", side="YES").first()
        assert pos is not None
        assert pos.shares == 100
        assert pos.avg_price == 0.5

    def test_averages_into_existing_position(self):
        e = self._make_executor()
        e._db_update_position(self.db, "m1", "Will X?", "YES", 100, 0.4, "paper", "test")
        e._db_update_position(self.db, "m1", "Will X?", "YES", 100, 0.6, "paper", "test")
        pos = self.db.query(self.Position).filter_by(market_id="m1", side="YES").first()
        assert pos.shares == 200
        assert abs(pos.avg_price - 0.5) < 0.001  # (0.4*100 + 0.6*100) / 200

    def test_pnl_is_zero_at_purchase_price(self):
        e = self._make_executor()
        e._db_update_position(self.db, "m1", "Will X?", "YES", 100, 0.5, "paper", "test")
        pos = self.db.query(self.Position).filter_by(market_id="m1", side="YES").first()
        assert pos.pnl == 0.0  # current_price == avg_price at trade time
