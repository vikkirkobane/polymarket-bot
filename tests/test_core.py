"""Unit tests for core bot logic."""

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

    def test_zero_volume_still_scored(self):
        r = self._make_researcher()
        market = {"yes_price": 0.5, "no_price": 0.5, "volume_24h": 0, "question": "test"}
        # vol_score=0 and deviation=0, but score ≥ 0
        assert r._quick_edge(market) >= 0

    def test_extreme_price_gets_boost(self):
        r = self._make_researcher()
        high = {"yes_price": 0.95, "no_price": 0.05, "volume_24h": 5_000, "question": "test"}
        normal = {"yes_price": 0.5, "no_price": 0.5, "volume_24h": 5_000, "question": "test"}
        assert r._quick_edge(high) > r._quick_edge(normal)

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


# ── analyst._repair_json ───────────────────────────────────────────────────────

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
    """Tests for the opposite-position closing logic using an in-memory SQLite DB."""

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
        pnl = e._db_close_opposite(self.db, "market1", "YES", 0.7)
        assert pnl == 0.0

    def test_closes_position_and_calculates_profit(self):
        e = self._make_executor()
        self._add_position("market1", "YES", 100, avg_price=0.5)
        pnl = e._db_close_opposite(self.db, "market1", "YES", 0.7)
        assert abs(pnl - 20.0) < 0.001  # (0.7 - 0.5) * 100 = 20

    def test_closes_position_and_calculates_loss(self):
        e = self._make_executor()
        self._add_position("market1", "NO", 50, avg_price=0.6)
        pnl = e._db_close_opposite(self.db, "market1", "NO", 0.4)
        assert abs(pnl - (-10.0)) < 0.001  # (0.4 - 0.6) * 50 = -10

    def test_fully_removes_position_from_db(self):
        e = self._make_executor()
        self._add_position("market1", "YES", 100, avg_price=0.5)
        e._db_close_opposite(self.db, "market1", "YES", 0.7)
        remaining = self.db.query(self.Position).filter_by(market_id="market1").first()
        assert remaining is None


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
