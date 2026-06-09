"""TradingAgents multi-agent runner.

Wraps TradingAgentsGraph so the rest of the app only sees:
    run_analysis(ticker, date) → AgentResult
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from app.config import settings

log = logging.getLogger(__name__)

# ── Rating → action mapping ────────────────────────────────────────────────
# TradingAgents uses a 5-tier PortfolioRating scale.
BUY_RATINGS = {"buy", "overweight"}
SELL_RATINGS = {"underweight", "sell"}


@dataclass
class AgentResult:
    ticker: str
    analysis_date: str
    rating: str          # raw rating string from PortfolioDecision
    action: str          # BUY | HOLD | SELL
    confidence: Optional[float]
    summary: str
    investment_thesis: str
    price_target: Optional[float]
    time_horizon: Optional[str]
    analyst_reports: Dict[str, str]   # analyst_name → report text
    raw_state: Dict[str, Any]


def _map_action(rating: str) -> str:
    r = rating.lower().strip()
    if r in BUY_RATINGS:
        return "BUY"
    if r in SELL_RATINGS:
        return "SELL"
    return "HOLD"


def _set_llm_env() -> None:
    """Ensure the chosen provider's API key is visible to TradingAgents."""
    key_map = {
        "groq": ("GROQ_API_KEY", settings.groq_api_key),
        "openai": ("OPENAI_API_KEY", settings.openai_api_key),
        "anthropic": ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
        "google": ("GOOGLE_API_KEY", settings.google_api_key),
        "openrouter": ("OPENROUTER_API_KEY", settings.openrouter_api_key),
    }
    env_var, key_val = key_map.get(settings.llm_provider, (None, None))
    if env_var and key_val:
        os.environ.setdefault(env_var, key_val)


def run_analysis(ticker: str, analysis_date: Optional[str] = None) -> AgentResult:
    """Run the full TradingAgents pipeline for *ticker* on *analysis_date*.

    This is a blocking call — wrap it in asyncio.to_thread() for async contexts.
    """
    _set_llm_env()

    trade_date = analysis_date or date.today().isoformat()

    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG
    except ImportError as exc:
        raise RuntimeError(
            "tradingagents package not installed. "
            "Run: pip install git+https://github.com/TauricResearch/TradingAgents.git"
        ) from exc

    config = {**DEFAULT_CONFIG}
    config["llm_provider"] = settings.llm_provider
    config["deep_think_llm"] = settings.deep_think_llm
    config["quick_think_llm"] = settings.quick_think_llm
    config["max_debate_rounds"] = settings.max_debate_rounds
    config["max_risk_discuss_rounds"] = settings.max_risk_rounds
    config["checkpoint_enabled"] = False  # stateless per-run in cloud
    config["output_language"] = "English"

    log.info("[%s] Starting TradingAgents analysis — provider=%s", ticker, settings.llm_provider)

    ta = TradingAgentsGraph(
        selected_analysts=["market", "sentiment", "news", "fundamentals"],
        debug=settings.debug,
        config=config,
    )

    final_state, decision = ta.propagate(ticker, trade_date, asset_type="stock")

    # ── Extract fields from PortfolioDecision (dataclass or Pydantic model) ──
    rating = str(getattr(decision, "rating", "hold"))
    summary = str(getattr(decision, "summary", ""))
    investment_thesis = str(getattr(decision, "investment_thesis", ""))
    price_target = getattr(decision, "price_target", None)
    time_horizon = getattr(decision, "time_horizon", None)

    # ── Extract per-analyst reports from final_state ──────────────────────
    analyst_reports = {}
    for analyst_key, state_key in [
        ("fundamental", "fundamentals_report"),
        ("technical", "market_report"),
        ("sentiment", "sentiment_report"),
        ("news", "news_report"),
    ]:
        val = final_state.get(state_key, "")
        if val:
            analyst_reports[analyst_key] = str(val)

    # ── Derive a rough confidence score from the debate history ───────────
    confidence = _estimate_confidence(final_state, rating)

    action = _map_action(rating)
    log.info("[%s] Decision: rating=%s action=%s confidence=%.2f", ticker, rating, action, confidence or 0)

    return AgentResult(
        ticker=ticker,
        analysis_date=trade_date,
        rating=rating,
        action=action,
        confidence=confidence,
        summary=summary,
        investment_thesis=investment_thesis,
        price_target=price_target,
        time_horizon=str(time_horizon) if time_horizon else None,
        analyst_reports=analyst_reports,
        raw_state=final_state,
    )


def _estimate_confidence(state: Dict[str, Any], rating: str) -> Optional[float]:
    """Heuristic: count how many analysts agree with the final rating direction."""
    try:
        r = rating.lower()
        bullish = r in BUY_RATINGS
        bearish = r in SELL_RATINGS
        if not (bullish or bearish):
            return 0.5

        # Look for keyword signals in analyst reports
        score = 0.0
        count = 0
        for key in ("fundamentals_report", "market_report", "sentiment_report", "news_report"):
            text = (state.get(key) or "").lower()
            if not text:
                continue
            count += 1
            bull_signals = text.count("bullish") + text.count("upside") + text.count("buy")
            bear_signals = text.count("bearish") + text.count("downside") + text.count("sell")
            if bullish:
                score += bull_signals / max(bull_signals + bear_signals, 1)
            else:
                score += bear_signals / max(bull_signals + bear_signals, 1)

        return round(score / count, 2) if count else 0.5
    except Exception:
        return None
