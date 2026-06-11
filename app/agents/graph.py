"""LangGraph multi-asset trading pipeline.

Graph flow per symbol:
  enrich → [market, sentiment, news, fundamentals, options_flow] (parallel)
         → bull_bear_debate  (loops up to max_debate_rounds if confidence low)
         → fund_manager
         → validator
         → execute_node

Supports: stocks, options, crypto, prediction markets.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Annotated
import operator

from langgraph.graph import StateGraph, END

from app.config import settings

log = logging.getLogger(__name__)

# ── Asset types Robinhood supports ───────────────────────────────────────────

ASSET_TYPE = Literal["stock", "option", "crypto", "prediction"]


# ── Graph state ───────────────────────────────────────────────────────────────

class TradingState(dict):
    """Typed dict that flows through every node."""
    # Inputs
    ticker: str
    asset_type: ASSET_TYPE
    analysis_date: str
    # Enrichment
    news_context: str
    options_flow_context: str
    macro_context: str
    memory_context: str
    market_snapshot: dict
    # Analyst reports (filled in parallel)
    market_report: str
    sentiment_report: str
    news_report: str
    fundamentals_report: str
    options_report: str
    # Debate
    bull_argument: str
    bear_argument: str
    debate_rounds: int
    # Decision
    rating: str
    action: str          # BUY | SELL | HOLD
    confidence: float
    investment_thesis: str
    price_target: Optional[float]
    time_horizon: Optional[str]
    # Validation
    validation_confidence: float
    validation_proceed: bool
    risk_summary: str
    validation_notes: str
    # Meta
    error: Optional[str]


def _default_state(ticker: str, asset_type: str, analysis_date: str) -> dict:
    return {
        "ticker": ticker,
        "asset_type": asset_type,
        "analysis_date": analysis_date,
        "news_context": "",
        "options_flow_context": "",
        "macro_context": "",
        "memory_context": "",
        "market_snapshot": {},
        "market_report": "",
        "sentiment_report": "",
        "news_report": "",
        "fundamentals_report": "",
        "options_report": "",
        "bull_argument": "",
        "bear_argument": "",
        "debate_rounds": 0,
        "rating": "Hold",
        "action": "HOLD",
        "confidence": 0.5,
        "investment_thesis": "",
        "price_target": None,
        "time_horizon": None,
        "validation_confidence": 0.5,
        "validation_proceed": True,
        "risk_summary": "",
        "validation_notes": "",
        "error": None,
    }


# ── LLM factory ──────────────────────────────────────────────────────────────

def _get_llm(quick: bool = False):
    model = settings.quick_think_llm if quick else settings.deep_think_llm
    provider = settings.llm_provider

    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=model, api_key=settings.groq_api_key, temperature=0.3)
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, temperature=0.3)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, temperature=0.3)
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, temperature=0.3)
    # fallback
    from langchain_groq import ChatGroq
    return ChatGroq(model=model, api_key=settings.groq_api_key, temperature=0.3)


# ── Nodes ─────────────────────────────────────────────────────────────────────

def node_enrich(state: dict) -> dict:
    """Pull live data from Robinhood MCP + Tavily search + Mem0 memory."""
    ticker = state["ticker"]
    asset_type = state["asset_type"]
    log.info("[%s] Enriching — Robinhood MCP + Tavily + Mem0", ticker)

    from app.agents.tools.search import search_ticker_news, search_options_flow, search_macro_context
    from app.agents.tools.memory import recall_ticker, recall_macro_lessons
    from app.broker.robinhood_mcp import robinhood

    from app.agents.tools.indicators import get_indicators
    from app.agents.tools.earnings import get_earnings_context
    from app.agents.tools.multiframe import get_multiframe_analysis
    from app.agents.tools.market_sentiment import get_macro_sentiment, get_ticker_crowd_sentiment
    from app.agents.tools.regime import get_regime_as_context
    from app.agents.tools.insider import get_insider_activity
    from app.agents.tools.unusual_whales import get_options_flow as get_uw_flow

    # Live market context from Robinhood (price, volume, VWAP, position, buying power, session)
    snapshot = robinhood.get_market_context(ticker, asset_type)

    # Technical indicators (RSI, MACD, Bollinger, RVOL, ATR)
    indicators = get_indicators(ticker, asset_type)

    # Multi-timeframe trend alignment (1h/4h/daily)
    mtf_text, mtf_confluence = get_multiframe_analysis(ticker, asset_type)

    # Fear & Greed + crowd sentiment (StockTwits / Reddit)
    macro_sentiment = get_macro_sentiment()
    crowd_sentiment = get_ticker_crowd_sentiment(ticker, asset_type)

    # Earnings calendar warning
    earnings_ctx = get_earnings_context(ticker) if asset_type == "stock" else ""

    # Market regime (VIX + HMM)
    regime_ctx = get_regime_as_context()

    # SEC insider activity (stocks only)
    insider_ctx = get_insider_activity(ticker) if asset_type == "stock" else ""

    # Unusual Whales options flow (replaces/augments basic options flow)
    uw_flow = get_uw_flow(ticker) if asset_type in ("stock", "option") else ""

    # Block trade if already holding and action would be a duplicate BUY
    if snapshot.get("already_holding"):
        log.info("[%s] Already holding position — agents will factor this in", ticker)

    # Asset-specific Tavily search
    if asset_type == "crypto":
        news = search_ticker_news(ticker, f"{ticker} crypto price prediction sentiment on-chain funding rate")
        flow = search_ticker_news(ticker, f"{ticker} whale large transaction exchange flow")
    elif asset_type == "option":
        underlying = ticker.split("_")[0] if "_" in ticker else ticker
        news = search_ticker_news(underlying, f"{underlying} options implied volatility earnings catalyst")
        flow = search_options_flow(underlying)
    elif asset_type == "prediction":
        news = search_ticker_news(ticker, f"{ticker} prediction market odds probability event")
        flow = ""
    else:
        news = search_ticker_news(ticker)
        flow = search_options_flow(ticker)

    macro = search_macro_context()
    memory = recall_ticker(ticker)
    macro_lessons = recall_macro_lessons()

    # Past analyst research from our own research library
    try:
        from app.database import get_research_summary
        past_research = get_research_summary(ticker)
    except Exception:
        past_research = ""

    vwap = snapshot.get("vwap", 0)
    price = snapshot.get("price", 0)
    session = snapshot.get("market_session", "regular")
    vwap_note = ""
    if vwap and price:
        vwap_note = f"\nVWAP: ${vwap:.2f}  Current: ${price:.2f}  {'ABOVE' if price > vwap else 'BELOW'} VWAP  Session: {session}"

    # Combine all context for news analyst
    full_news = "\n\n".join(filter(None, [news, vwap_note, indicators, mtf_text, earnings_ctx, crowd_sentiment, insider_ctx]))

    return {
        **state,
        "news_context": full_news,
        "options_flow_context": flow,
        "macro_context": "\n\n".join(filter(None, [macro, macro_lessons, macro_sentiment, regime_ctx])),
        "memory_context": "\n\n".join(filter(None, [memory, past_research])),
        "market_snapshot": {**snapshot, "mtf_confluence": mtf_confluence},
        "options_flow_context": uw_flow or state.get("options_flow_context", ""),
    }


def _analyst_prompt(role: str, ticker: str, asset_type: str, context: str, extra: str = "") -> str:
    asset_note = {
        "stock": "This is an equity/stock.",
        "option": "This is an options contract. Consider implied volatility, Greeks, time decay, and the catalyst.",
        "crypto": "This is a cryptocurrency. Consider on-chain data, sentiment, and macro crypto conditions.",
        "prediction": "This is a prediction market contract. Consider probability, event risk, and market odds.",
    }.get(asset_type, "")

    return f"""You are a {role} analyst. {asset_note}

Ticker: {ticker}
{context}
{extra}

Write a concise 150-word analysis from your perspective. End with a clear directional bias: BULLISH, BEARISH, or NEUTRAL."""


def node_market_analyst(state: dict) -> dict:
    ticker = state["ticker"]
    snap = state["market_snapshot"]
    price_info = f"Price: ${snap.get('price', 'N/A')}  Change: {snap.get('change_pct', 'N/A')}%  Volume: {snap.get('volume', 'N/A')}" if snap else ""
    prompt = _analyst_prompt(
        "technical/market",
        ticker,
        state["asset_type"],
        f"Market Data:\n{price_info}\n\nMacro Context:\n{state['macro_context']}",
    )
    try:
        report = _get_llm(quick=True).invoke(prompt).content
    except Exception as exc:
        report = f"Market analysis unavailable: {exc}"
    log.debug("[%s] Market analyst done", ticker)
    return {**state, "market_report": report}


def node_sentiment_analyst(state: dict) -> dict:
    ticker = state["ticker"]
    prompt = _analyst_prompt(
        "sentiment/social",
        ticker,
        state["asset_type"],
        f"News & Social Context:\n{state['news_context']}\n\nPast Memory:\n{state['memory_context']}",
    )
    try:
        report = _get_llm(quick=True).invoke(prompt).content
    except Exception as exc:
        report = f"Sentiment analysis unavailable: {exc}"
    log.debug("[%s] Sentiment analyst done", ticker)
    return {**state, "sentiment_report": report}


def node_news_analyst(state: dict) -> dict:
    ticker = state["ticker"]
    prompt = _analyst_prompt(
        "news/catalyst",
        ticker,
        state["asset_type"],
        f"Latest News:\n{state['news_context']}",
        extra="Focus on: earnings, product launches, regulatory news, macro catalysts.",
    )
    try:
        report = _get_llm(quick=True).invoke(prompt).content
    except Exception as exc:
        report = f"News analysis unavailable: {exc}"
    log.debug("[%s] News analyst done", ticker)
    return {**state, "news_report": report}


def node_fundamentals_analyst(state: dict) -> dict:
    ticker = state["ticker"]
    asset_type = state["asset_type"]
    if asset_type in ("crypto", "prediction"):
        extra = "For crypto: focus on tokenomics, adoption, on-chain metrics, and protocol fundamentals."
    else:
        extra = "Focus on: P/E, revenue growth, margins, balance sheet strength, competitive moat."
    prompt = _analyst_prompt(
        "fundamentals/valuation",
        ticker,
        asset_type,
        f"Context:\n{state['news_context']}",
        extra=extra,
    )
    try:
        report = _get_llm(quick=True).invoke(prompt).content
    except Exception as exc:
        report = f"Fundamentals analysis unavailable: {exc}"
    log.debug("[%s] Fundamentals analyst done", ticker)
    return {**state, "fundamentals_report": report}


def node_options_flow_analyst(state: dict) -> dict:
    ticker = state["ticker"]
    if not state["options_flow_context"]:
        return {**state, "options_report": "No options flow data available."}
    prompt = _analyst_prompt(
        "options flow / smart money",
        ticker,
        state["asset_type"],
        f"Options Flow & Dark Pool Data:\n{state['options_flow_context']}",
        extra="Identify: unusual call/put activity, large block trades, dark pool prints, institutional positioning.",
    )
    try:
        report = _get_llm(quick=True).invoke(prompt).content
    except Exception as exc:
        report = f"Options flow analysis unavailable: {exc}"
    log.debug("[%s] Options flow analyst done", ticker)
    return {**state, "options_report": report}


def node_bull_bear_debate(state: dict) -> dict:
    """Bull and Bear agents debate — uses deep LLM for quality reasoning."""
    ticker = state["ticker"]
    rounds = state["debate_rounds"]
    log.info("[%s] Bull/Bear debate round %d", ticker, rounds + 1)

    reports_summary = f"""
MARKET ANALYST: {state['market_report'][:300]}
SENTIMENT ANALYST: {state['sentiment_report'][:300]}
NEWS ANALYST: {state['news_report'][:300]}
FUNDAMENTALS ANALYST: {state['fundamentals_report'][:300]}
OPTIONS FLOW: {state['options_report'][:200]}
"""

    prior_debate = ""
    if rounds > 0:
        prior_debate = f"\n\nPRIOR DEBATE:\nBull: {state['bull_argument'][:200]}\nBear: {state['bear_argument'][:200]}\n\nRefine your arguments based on the prior round."

    bull_prompt = f"""You are the BULL. Argue strongly WHY {ticker} is a strong BUY right now.
Use the analyst reports below. Be specific — cite data points, catalysts, momentum.
{reports_summary}{prior_debate}
Make your bull case in 150 words."""

    bear_prompt = f"""You are the BEAR. Argue strongly WHY {ticker} is a SELL or AVOID right now.
Use the analyst reports below. Identify risks, overvaluation, negative catalysts.
{reports_summary}{prior_debate}
Make your bear case in 150 words."""

    llm = _get_llm(quick=False)
    try:
        bull = llm.invoke(bull_prompt).content
    except Exception as exc:
        bull = f"Bull argument unavailable: {exc}"
    try:
        bear = llm.invoke(bear_prompt).content
    except Exception as exc:
        bear = f"Bear argument unavailable: {exc}"

    return {**state, "bull_argument": bull, "bear_argument": bear, "debate_rounds": rounds + 1}


def node_fund_manager(state: dict) -> dict:
    """Fund Manager weighs all inputs and makes the final decision."""
    ticker = state["ticker"]
    asset_type = state["asset_type"]
    log.info("[%s] Fund Manager deciding", ticker)

    asset_note = {
        "stock": "",
        "option": "For options: also specify contract type (call/put), strike, and expiry recommendation.",
        "crypto": "For crypto: consider position sizing given higher volatility.",
        "prediction": "For prediction markets: evaluate the probability vs the market price.",
    }.get(asset_type, "")

    snap = state.get("market_snapshot", {})
    portfolio_note = ""
    mtf = snap.get("mtf_confluence", 0.0)
    mtf_note = ""
    if mtf >= 0.67:
        mtf_note = "\n✅ MULTI-TIMEFRAME: All timeframes aligned BULLISH — higher conviction for BUY."
    elif mtf <= -0.67:
        mtf_note = "\n❌ MULTI-TIMEFRAME: All timeframes aligned BEARISH — avoid BUY, lean SELL/HOLD."
    elif abs(mtf) < 0.34:
        mtf_note = "\n⚠️ MULTI-TIMEFRAME: Timeframes conflicted — reduce size or wait for alignment."

    if snap.get("already_holding"):
        pos = snap.get("current_position") or {}
        qty = pos.get("quantity") or pos.get("shares_held_for_sells") or "unknown"
        avg_price = pos.get("average_buy_price") or "unknown"
        portfolio_note = f"\n⚠️ PORTFOLIO: Already holding {qty} units of {ticker} at avg ${avg_price}. Do NOT issue another BUY unless clearly adding to a winning position is justified. Prefer HOLD or SELL."

    prompt = f"""You are the Fund Manager. Make the FINAL trading decision for {ticker}.

BULL CASE: {state['bull_argument']}

BEAR CASE: {state['bear_argument']}

ANALYST REPORTS SUMMARY:
- Market: {state['market_report'][:200]}
- Sentiment: {state['sentiment_report'][:200]}
- News: {state['news_report'][:200]}
- Fundamentals: {state['fundamentals_report'][:200]}
- Options Flow: {state['options_report'][:150]}

PAST MEMORY: {state['memory_context'][:200]}
{portfolio_note}{mtf_note}
{asset_note}

Respond in EXACTLY this format:
ACTION: [BUY or SELL or HOLD]
RATING: [Strong Buy / Buy / Overweight / Hold / Underweight / Sell / Strong Sell]
CONFIDENCE: [0.0-1.0]
PRICE_TARGET: [number or N/A]
STOP_LOSS: [price level to exit if wrong, or N/A]
TAKE_PROFIT: [price level to take gains, or N/A]
TIME_HORIZON: [e.g. "1-2 weeks" or N/A]
THESIS: [2-3 sentence investment thesis explaining the decision]"""

    try:
        response = _get_llm(quick=False).invoke(prompt).content
        result = _parse_fund_manager(response)
    except Exception as exc:
        log.error("[%s] Fund manager failed: %s", ticker, exc)
        result = {
            "action": "HOLD", "rating": "Hold", "confidence": 0.5,
            "investment_thesis": f"Analysis error: {exc}",
            "price_target": None, "time_horizon": None,
        }

    return {**state, **result}


def _parse_fund_manager(text: str) -> dict:
    result = {
        "action": "HOLD", "rating": "Hold", "confidence": 0.5,
        "investment_thesis": "", "price_target": None, "time_horizon": None,
        "stop_loss": None, "take_profit": None,
    }
    thesis_lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("ACTION:"):
            val = line.split(":", 1)[1].strip().upper()
            if val in ("BUY", "SELL", "HOLD"):
                result["action"] = val
        elif line.startswith("RATING:"):
            result["rating"] = line.split(":", 1)[1].strip()
        elif line.startswith("CONFIDENCE:"):
            try:
                result["confidence"] = max(0.0, min(1.0, float(line.split(":", 1)[1].strip())))
            except ValueError:
                pass
        elif line.startswith("PRICE_TARGET:"):
            val = line.split(":", 1)[1].strip()
            if val.upper() != "N/A":
                try:
                    result["price_target"] = float(val.replace("$", "").replace(",", ""))
                except ValueError:
                    pass
        elif line.startswith("STOP_LOSS:"):
            val = line.split(":", 1)[1].strip()
            if val.upper() != "N/A":
                try:
                    result["stop_loss"] = float(val.replace("$", "").replace(",", ""))
                except ValueError:
                    pass
        elif line.startswith("TAKE_PROFIT:"):
            val = line.split(":", 1)[1].strip()
            if val.upper() != "N/A":
                try:
                    result["take_profit"] = float(val.replace("$", "").replace(",", ""))
                except ValueError:
                    pass
        elif line.startswith("TIME_HORIZON:"):
            val = line.split(":", 1)[1].strip()
            result["time_horizon"] = None if val.upper() == "N/A" else val
        elif line.startswith("THESIS:"):
            thesis_lines.append(line.split(":", 1)[1].strip())
        elif thesis_lines:
            thesis_lines.append(line)

    result["investment_thesis"] = " ".join(thesis_lines).strip() or text[:300]
    return result


def node_validator(state: dict) -> dict:
    """Devil's Advocate stress-test before any order is placed."""
    ticker = state["ticker"]
    if state["action"] == "HOLD":
        return {**state, "validation_confidence": 1.0, "validation_proceed": True,
                "risk_summary": "HOLD — no trade to validate.", "validation_notes": ""}

    log.info("[%s] Validator stress-testing %s decision", ticker, state["action"])

    from app.agents.validator import validate_decision
    analyst_reports = {
        "market": state["market_report"],
        "sentiment": state["sentiment_report"],
        "news": state["news_report"],
        "fundamentals": state["fundamentals_report"],
        "options_flow": state["options_report"],
    }
    result = validate_decision(
        ticker=ticker,
        action=state["action"],
        rating=state["rating"],
        investment_thesis=state["investment_thesis"],
        analyst_reports=analyst_reports,
    )
    return {
        **state,
        "validation_confidence": result.confidence,
        "validation_proceed": result.proceed,
        "risk_summary": result.risk_summary,
        "validation_notes": result.validation_notes,
    }


# ── Conditional edges ─────────────────────────────────────────────────────────

def should_redebate(state: dict) -> str:
    """Loop back for another debate round if confidence is low."""
    max_rounds = getattr(settings, "max_debate_rounds", 2)
    confidence = state.get("confidence", 0.5)
    rounds = state.get("debate_rounds", 0)

    if confidence < 0.55 and rounds < max_rounds:
        log.info("[%s] Confidence %.2f low — re-debating (round %d/%d)",
                 state["ticker"], confidence, rounds, max_rounds)
        return "redebate"
    return "continue"


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_graph() -> Any:
    g = StateGraph(dict)

    g.add_node("enrich", node_enrich)
    g.add_node("market_analyst", node_market_analyst)
    g.add_node("sentiment_analyst", node_sentiment_analyst)
    g.add_node("news_analyst", node_news_analyst)
    g.add_node("fundamentals_analyst", node_fundamentals_analyst)
    g.add_node("options_flow_analyst", node_options_flow_analyst)
    g.add_node("bull_bear_debate", node_bull_bear_debate)
    g.add_node("fund_manager", node_fund_manager)
    g.add_node("validator", node_validator)

    # Enrich → all 5 analysts (fan-out)
    g.set_entry_point("enrich")
    for analyst in ["market_analyst", "sentiment_analyst", "news_analyst",
                    "fundamentals_analyst", "options_flow_analyst"]:
        g.add_edge("enrich", analyst)

    # All analysts → debate (fan-in)
    for analyst in ["market_analyst", "sentiment_analyst", "news_analyst",
                    "fundamentals_analyst", "options_flow_analyst"]:
        g.add_edge(analyst, "bull_bear_debate")

    # Debate → fund manager (with optional re-debate loop)
    g.add_edge("bull_bear_debate", "fund_manager")
    g.add_conditional_edges(
        "fund_manager",
        should_redebate,
        {"redebate": "bull_bear_debate", "continue": "validator"},
    )
    g.add_edge("validator", END)

    return g.compile()


_graph = None

def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class GraphResult:
    ticker: str
    asset_type: str
    analysis_date: str
    action: str
    rating: str
    confidence: float
    investment_thesis: str
    price_target: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    time_horizon: Optional[str]
    validation_confidence: float
    validation_proceed: bool
    risk_summary: str
    analyst_reports: Dict[str, str]
    raw_state: dict


def run_graph(ticker: str, asset_type: str, analysis_date: str) -> GraphResult:
    """Run the full LangGraph pipeline for one symbol. Blocking — use asyncio.to_thread."""
    _set_llm_env()
    graph = get_graph()

    initial = _default_state(ticker, asset_type, analysis_date)
    log.info("[%s] Starting LangGraph pipeline (asset_type=%s)", ticker, asset_type)

    final = graph.invoke(initial)

    return GraphResult(
        ticker=ticker,
        asset_type=asset_type,
        analysis_date=analysis_date,
        action=final.get("action", "HOLD"),
        rating=final.get("rating", "Hold"),
        confidence=final.get("confidence", 0.5),
        investment_thesis=final.get("investment_thesis", ""),
        price_target=final.get("price_target"),
        stop_loss=final.get("stop_loss"),
        take_profit=final.get("take_profit"),
        time_horizon=final.get("time_horizon"),
        validation_confidence=final.get("validation_confidence", 0.5),
        validation_proceed=final.get("validation_proceed", True),
        risk_summary=final.get("risk_summary", ""),
        analyst_reports={
            "market": final.get("market_report", ""),
            "sentiment": final.get("sentiment_report", ""),
            "news": final.get("news_report", ""),
            "fundamentals": final.get("fundamentals_report", ""),
            "options_flow": final.get("options_report", ""),
        },
        raw_state=final,
    )


def _set_llm_env() -> None:
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
