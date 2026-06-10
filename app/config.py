"""Central configuration — loaded from environment variables and .env file."""
from __future__ import annotations

from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── LLM ──────────────────────────────────────────────────────────────────
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    llm_provider: str = "groq"
    deep_think_llm: str = "llama-3.3-70b-versatile"
    quick_think_llm: str = "llama-3.1-8b-instant"

    # ── Robinhood / Vibe-Trading ──────────────────────────────────────────────
    robinhood_mcp_url: str = "http://robinhood-mcp:8765"
    vibe_trading_runtime_root: str = "/data/vibe-runtime"
    vibe_trading_api_key: Optional[str] = None

    # ── Mandate ───────────────────────────────────────────────────────────────
    mandate_max_order_usd: float = 500.0
    mandate_daily_cap_usd: float = 2000.0
    mandate_allowed_symbols: str = "AAPL,MSFT,GOOGL"
    mandate_max_exposure_usd: float = 10_000.0
    mandate_max_trades_per_day: int = 5

    # ── Market Scanner ────────────────────────────────────────────────────────
    polygon_api_key: Optional[str] = None
    scanner_max_tickers: int = 7
    scanner_min_price: float = 5.0
    scanner_min_volume: int = 500_000
    scanner_crypto_symbols: str = "BTC,ETH,SOL"    # crypto always scanned
    scanner_options_enabled: bool = False           # enable options scanning
    prediction_symbols: str = ""                    # comma-separated prediction contracts

    # ── Live Research (Tavily) ────────────────────────────────────────────────
    tavily_api_key: Optional[str] = None            # free at app.tavily.com

    # ── Agent Memory (Mem0) ───────────────────────────────────────────────────
    mem0_api_key: Optional[str] = None              # free at app.mem0.ai

    # ── Scheduler ─────────────────────────────────────────────────────────────
    loop_interval_minutes: int = 60        # full market scan interval
    crypto_loop_interval_minutes: int = 20 # crypto-only loop (runs 24/7)
    analysis_date_override: Optional[str] = None
    max_debate_rounds: int = 2
    max_risk_rounds: int = 1

    # ── Notifications ─────────────────────────────────────────────────────────
    discord_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # ── External Alpha Data ───────────────────────────────────────────────────
    unusual_whales_api_key: Optional[str] = None       # unusualwhales.com — options flow

    # ── Risk Controls ─────────────────────────────────────────────────────────
    max_drawdown_halt_pct: float = 0.05    # halt if portfolio drops 5% in a day
    correlation_max_overlap: float = 0.75  # block new BUY if existing position correlation > 0.75

    # ── Webhooks ──────────────────────────────────────────────────────────────
    tradingview_webhook_secret: Optional[str] = None   # set to lock down /webhook/tradingview

    # ── App ───────────────────────────────────────────────────────────────────
    debug: bool = False
    database_path: str = "/data/trading_audit.db"
    dry_run: bool = False

    @field_validator("mandate_allowed_symbols", mode="before")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def allowed_symbols(self) -> List[str]:
        return [s.strip().upper() for s in self.mandate_allowed_symbols.split(",") if s.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
