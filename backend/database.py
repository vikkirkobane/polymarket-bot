"""backend/database.py — SQLAlchemy models + session factory."""

from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, create_engine, text
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"
DB_PATH.parent.mkdir(exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    market_id = Column(String, index=True)
    question = Column(Text)
    side = Column(String)           # YES | NO
    size = Column(Float)            # USD
    price = Column(Float)           # 0–1
    shares = Column(Float, default=0.0)
    status = Column(String)         # pending | filled | failed | closed
    pnl = Column(Float, nullable=True)
    mode = Column(String)           # paper | live
    source = Column(String)         # research | copy | manual
    model_used = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    market_id = Column(String, unique=True, index=True)
    question = Column(Text)
    side = Column(String)
    shares = Column(Float)
    avg_price = Column(Float)
    current_price = Column(Float, default=0.0)
    pnl = Column(Float, default=0.0)
    mode = Column(String)
    source = Column(String)
    opened_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CopyTrader(Base):
    __tablename__ = "copy_traders"
    id = Column(Integer, primary_key=True)
    address = Column(String, unique=True, index=True)
    name = Column(String, nullable=True)
    roi = Column(Float, default=0.0)
    win_rate = Column(Float, default=0.0)
    total_profit = Column(Float, default=0.0)
    volume = Column(Float, default=0.0)
    active = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.utcnow)


class CopySignal(Base):
    __tablename__ = "copy_signals"
    id = Column(Integer, primary_key=True)
    trader_address = Column(String)
    trader_name = Column(String, nullable=True)
    market_id = Column(String)
    question = Column(Text)
    side = Column(String)
    original_size = Column(Float)
    copied_size = Column(Float)
    price = Column(Float)
    status = Column(String)         # pending | executed | skipped
    timestamp = Column(DateTime, default=datetime.utcnow)
    outcome = Column(String, nullable=True)           # 'success' | 'failure'
    outcome_marked_at = Column(DateTime, nullable=True)


class MarketOpportunity(Base):
    __tablename__ = "market_opportunities"
    id = Column(Integer, primary_key=True)
    market_id = Column(String, index=True)
    question = Column(Text)
    yes_price = Column(Float)
    no_price = Column(Float)
    volume_24h = Column(Float, default=0.0)
    edge_pct = Column(Float, default=0.0)
    side = Column(String)
    confidence = Column(String)     # low | medium | high
    model_used = Column(String)
    analysis = Column(Text)
    trader_matches = Column(Text)   # JSON array of trader names/addresses aligned with this trade
    end_date = Column(String)       # Event date for sorting pending approvals
    scanned_at = Column(DateTime, default=datetime.utcnow)
    outcome = Column(String, nullable=True)           # 'success' | 'failure'
    outcome_marked_at = Column(DateTime, nullable=True)
    flagged_for_approval = Column(Boolean, default=False)


class TraderPositionState(Base):
    """Persist last seen positions for copy traders to survive restarts."""
    __tablename__ = "trader_position_states"
    id = Column(Integer, primary_key=True)
    trader_address = Column(String, index=True)
    market_id = Column(String, index=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow)


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    id           = Column(Integer, primary_key=True)
    entry_type   = Column(String)                  # opportunity | trade | copy_signal | manual
    source_id    = Column(Integer, nullable=True)  # id from source table
    market_id    = Column(String, index=True)
    question     = Column(Text)
    side         = Column(String)                  # YES | NO
    price        = Column(Float, nullable=True)    # entry price at journaling time
    edge_pct     = Column(Float, nullable=True)
    confidence   = Column(String, nullable=True)   # low | medium | high
    notes        = Column(Text, nullable=True)
    tags         = Column(Text, nullable=True)     # JSON array e.g. '["geopolitics","high-edge"]'
    rating       = Column(Integer, nullable=True)  # 1–5
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)
    # SQLite column migration — add new columns if they don't exist yet
    with engine.connect() as conn:
        for stmt in [
            "ALTER TABLE market_opportunities ADD COLUMN outcome VARCHAR",
            "ALTER TABLE market_opportunities ADD COLUMN outcome_marked_at DATETIME",
            "ALTER TABLE market_opportunities ADD COLUMN flagged_for_approval BOOLEAN DEFAULT 0",
            "ALTER TABLE copy_signals ADD COLUMN outcome VARCHAR",
            "ALTER TABLE copy_signals ADD COLUMN outcome_marked_at DATETIME",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass  # Column already exists


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
