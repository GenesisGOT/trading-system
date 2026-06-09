"""Central configuration — loaded from environment variables and .env file."""
from __future__ import annotations

from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── LLM ──────────────────────────────────────────────────────────────────
    # Default: Groq (free tier — no credit card, 30 RPM, Llama 3.3 70B)
    # Alternatives: openai | anthropic | google | deepseek | openrouter
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None          # free at console.groq.com
    google_api_key: Optional[str] = None        # free at aistudio.google.com
    openrouter_api_key: Optional[str] = None    # free models at openrouter.ai
    llm_provider: str = "groq"
    deep_think_llm: str = "llama-3.3-70b-versatile"   # Groq free — great for debates
    quick_think_llm: str = "llama-3.1-8b-instant"     # Groq free — fast analyst passes

    # ── Robinhood / Vibe-Trading ─────────────────────────────────────────────
    robinhood_mcp_url: str = "http://robinhood-mcp:8765"
    vibe_trading_runtime_root: str = "/data/vibe-runtime"
    vibe_trading_api_key: Optional[str] = None  # bearer token for vibe api_server

    # ── Mandate (hard caps enforced before any order reaches the broker) ─────
    mandate_max_order_usd: float = 500.0
    mandate_daily_cap_usd: float = 2000.0
    mandate_allowed_symbols: str = "AAPL,MSFT,GOOGL"  # comma-separated
    mandate_max_exposure_usd: float = 10_000.0
    mandate_max_trades_per_day: int = 5

    # ── Scheduler ────────────────────────────────────────────────────────────
    loop_interval_minutes: int = 60
    analysis_date_override: Optional[str] = None  # YYYY-MM-DD; defaults to today
    max_debate_rounds: int = 1
    max_risk_rounds: int = 1

    # ── Notifications ────────────────────────────────────────────────────────
    discord_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # ── App ──────────────────────────────────────────────────────────────────
    debug: bool = False
    database_path: str = "/data/trading_audit.db"
    dry_run: bool = False  # log decisions but never submit orders

    @field_validator("mandate_allowed_symbols", mode="before")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def allowed_symbols(self) -> List[str]:
        return [s.strip().upper() for s in self.mandate_allowed_symbols.split(",") if s.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
