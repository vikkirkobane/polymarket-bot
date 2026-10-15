"""backend/config.py — Pydantic-settings configuration."""

from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    # Trading mode
    paper_mode: bool = Field(True, alias="PAPER_MODE")
    paper_balance: float = Field(10000.0, alias="PAPER_BALANCE")

    # AI model selection
    ai_model: str = Field("claude", alias="AI_MODEL")
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field("", alias="OPENAI_API_KEY")
    google_api_key: str = Field("", alias="GOOGLE_API_KEY")
    openrouter_api_key: str = Field("", alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field("nvidia/nemotron-3-super-120b-a12b:free", alias="OPENROUTER_MODEL")
    # Concrete provider models (override only if a provider renames/deprecates defaults)
    anthropic_model: str = Field("claude-sonnet-4-5", alias="ANTHROPIC_MODEL")
    openai_model: str = Field("gpt-4o", alias="OPENAI_MODEL")
    gemini_model: str = Field("gemini-3-flash", alias="GEMINI_MODEL")

    # Polymarket credentials
    poly_api_key: str = Field("", alias="POLY_API_KEY")
    poly_api_secret: str = Field("", alias="POLY_API_SECRET")
    poly_passphrase: str = Field("", alias="POLY_PASSPHRASE")
    poly_private_key: str = Field("", alias="POLY_PRIVATE_KEY")
    poly_wallet_address: str = Field("", alias="POLY_WALLET_ADDRESS")

    # Copy trading
    copy_trading_enabled: bool = Field(True, alias="COPY_TRADING_ENABLED")
    copy_scale_factor: float = Field(0.1, alias="COPY_SCALE_FACTOR")
    max_copy_traders: int = Field(10, alias="MAX_COPY_TRADERS")

    # Risk management
    min_edge_percent: float = Field(5.0, alias="MIN_EDGE_PERCENT")
    max_bet_size: float = Field(100.0, alias="MAX_BET_SIZE")
    max_daily_spend: float = Field(500.0, alias="MAX_DAILY_SPEND")
    min_volume: float = Field(1000.0, alias="MIN_VOLUME")
    auto_execute: bool = Field(False, alias="AUTO_EXECUTE")

    # Risk layer (sizing / exposure / drawdown protection)
    kelly_fraction: float = Field(0.25, alias="KELLY_FRACTION")           # fraction of full Kelly to bet
    max_position_pct: float = Field(5.0, alias="MAX_POSITION_PCT")        # hard cap per trade, % of bankroll
    max_open_positions: int = Field(10, alias="MAX_OPEN_POSITIONS")
    max_event_exposure_pct: float = Field(10.0, alias="MAX_EVENT_EXPOSURE_PCT")  # correlated (negRisk) group cap
    max_total_exposure_pct: float = Field(50.0, alias="MAX_TOTAL_EXPOSURE_PCT")
    weekly_drawdown_limit_pct: float = Field(10.0, alias="WEEKLY_DRAWDOWN_LIMIT_PCT")  # auto-pause bot
    max_spread_edge_ratio: float = Field(0.5, alias="MAX_SPREAD_EDGE_RATIO")  # skip if spread > ratio*edge
    use_kelly_sizing: bool = Field(True, alias="USE_KELLY_SIZING")

    # Position monitoring
    monitor_interval_minutes: int = Field(10, alias="POSITION_MONITOR_INTERVAL_MINUTES")
    monitor_auto_exit: bool = Field(True, alias="MONITOR_AUTO_EXIT")            # paper mode
    monitor_auto_exit_live: bool = Field(False, alias="MONITOR_AUTO_EXIT_LIVE")  # opt-in for real money
    stop_loss_pct: float = Field(25.0, alias="STOP_LOSS_PCT")                    # exit when down N% of stake
    edge_evaporation_margin_pct: float = Field(1.5, alias="EDGE_EVAPORATION_MARGIN_PCT")
    near_resolution_price: float = Field(0.98, alias="NEAR_RESOLUTION_PRICE")

    # Corroboration
    momentum_lookback_hours: int = Field(24, alias="MOMENTUM_LOOKBACK_HOURS")

    # Scheduling
    scan_interval_minutes: int = Field(15, alias="SCAN_INTERVAL_MINUTES")
    copy_poll_interval_minutes: int = Field(5, alias="COPY_POLL_INTERVAL_MINUTES")

    # Server
    port: int = Field(8000, alias="PORT")

    class Config:
        env_file = ".env"
        populate_by_name = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()
