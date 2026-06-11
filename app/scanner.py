"""Market scanner — Robinhood-first data source.

Pulls top movers, crypto, options flow, prediction markets, and news
directly from Robinhood MCP. Falls back to config symbols if MCP unavailable.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from app.config import settings

log = logging.getLogger(__name__)

MIN_PRICE  = 2.0
MIN_VOLUME = 100_000

# Static fallback crypto watchlist
CRYPTO_WATCHLIST = ["BTC", "ETH", "SOL", "DOGE", "AVAX"]

# Common non-ticker words to filter out of news extraction
_STOPWORDS = {
    "THE", "FOR", "AND", "BUT", "NOT", "ARE", "WAS", "HAS", "ITS", "NEW",
    "ALL", "USD", "ETF", "IPO", "CEO", "CFO", "SEC", "FDA", "EPS", "YOY",
    "QOQ", "LLC", "INC", "LTD", "EST", "EDT", "PST", "UTC", "API", "GDP",
    "CPI", "PCE", "NFP", "ATH", "ATL", "ROI", "P&L", "PNL", "YTD", "MTD",
}


def _rh():
    from app.broker.robinhood_mcp import robinhood
    return robinhood


# ── Stocks ────────────────────────────────────────────────────────────────────

def _scan_stocks() -> List[Tuple[str, str]]:
    """Pull top movers from Robinhood MCP."""
    try:
        rh = _rh()

        # Try Robinhood top movers / most popular
        movers = rh._call("get_top_movers", {"direction": "up", "limit": 20}) or {}
        losers = rh._call("get_top_movers", {"direction": "down", "limit": 10}) or {}
        popular = rh._call("get_most_popular", {"limit": 10}) or {}

        candidates = []
        for group in [movers, losers, popular]:
            items = group.get("results", group.get("instruments", []))
            if isinstance(items, list):
                for item in items:
                    sym = (item.get("symbol") or item.get("ticker") or "").upper()
                    price = float(item.get("price") or item.get("last_trade_price") or 0)
                    volume = float(item.get("volume") or 0)
                    if sym and price >= MIN_PRICE and volume >= MIN_VOLUME:
                        candidates.append(sym)

        # Deduplicate and cap
        seen, result = set(), []
        for sym in candidates:
            if sym not in seen and sym not in _STOPWORDS:
                seen.add(sym)
                result.append((sym, "stock"))

        max_stocks = max(3, settings.scanner_max_tickers - 3)
        if result:
            log.info("Robinhood scanner: %d stocks found", len(result))
            return result[:max_stocks]

    except Exception as exc:
        log.warning("Robinhood stock scanner failed: %s — using config symbols", exc)

    return [(s, "stock") for s in settings.allowed_symbols]


# ── Crypto ────────────────────────────────────────────────────────────────────

def _scan_crypto() -> List[Tuple[str, str]]:
    """Pull active crypto — always returns config symbols, enriches with Robinhood if available."""
    enabled = [s.strip().upper() for s in settings.scanner_crypto_symbols.split(",") if s.strip()]
    if not enabled:
        enabled = ["BTC", "ETH", "SOL"]  # hard fallback

    # Always return config symbols — Robinhood quote check is optional enrichment
    try:
        from app.broker.alpaca_connector import get_quote as alpaca_quote
        result = []
        for sym in enabled:
            try:
                q = alpaca_quote(sym + "USD" if not sym.endswith("USD") else sym)
                if q:
                    result.append((sym, "crypto"))
                    continue
            except Exception:
                pass
            result.append((sym, "crypto"))  # include even if quote fails
        return result
    except Exception:
        pass

    return [(s, "crypto") for s in enabled]


# ── Options ───────────────────────────────────────────────────────────────────

def _scan_options() -> List[Tuple[str, str]]:
    """Find unusual options activity via Robinhood options chain data."""
    if not settings.scanner_options_enabled:
        return []

    try:
        rh = _rh()
        unusual = []

        # Check options chains on our top stock picks for unusual volume
        stock_picks = [s for s in settings.allowed_symbols[:5]]
        for ticker in stock_picks:
            try:
                chain = rh.get_options_chain(ticker)
                if not chain:
                    continue
                for contract in chain[:20]:
                    oi = float(contract.get("open_interest") or 0)
                    vol = float(contract.get("volume") or 0)
                    if oi > 0 and vol / max(oi, 1) > 2.0:  # volume > 2x open interest = unusual
                        unusual.append((ticker, "option"))
                        break
            except Exception:
                continue

        return unusual[:2]

    except Exception as exc:
        log.warning("Options scanner failed: %s", exc)
        return []


# ── Prediction Markets ────────────────────────────────────────────────────────

def _scan_predictions() -> List[Tuple[str, str]]:
    """Pull active prediction market contracts from Robinhood."""
    # First check config overrides
    config_symbols = [s.strip() for s in settings.prediction_symbols.split(",") if s.strip()]

    try:
        rh = _rh()
        # Try to pull live prediction contracts from Robinhood
        contracts = rh._call("get_prediction_contracts", {"status": "open"}) or {}
        items = contracts.get("results", contracts.get("contracts", []))

        if isinstance(items, list) and items:
            active = []
            for c in items:
                sym = (c.get("symbol") or c.get("event_id") or "").upper()
                vol = float(c.get("volume") or c.get("contracts_traded") or 0)
                if sym and vol > 100:  # only contracts with real activity
                    active.append((sym, "prediction"))

            if active:
                log.info("Robinhood prediction markets: %d active contracts", len(active))
                return active[:3]

    except Exception as exc:
        log.warning("Prediction market scanner failed: %s", exc)

    return [(s, "prediction") for s in config_symbols]


# ── News scanner ──────────────────────────────────────────────────────────────

def get_robinhood_news(ticker: str) -> str:
    """Pull news articles directly from Robinhood for a ticker."""
    try:
        rh = _rh()
        result = rh._call("get_news", {"symbol": ticker, "limit": 10}) or {}
        articles = result.get("results", result.get("news", []))

        if not articles:
            return ""

        lines = [f"[Robinhood News for {ticker}]"]
        for a in articles[:8]:
            title = a.get("title") or a.get("headline") or ""
            source = a.get("source") or a.get("publisher", {}).get("name") or ""
            summary = a.get("summary") or a.get("preview_text") or ""
            published = (a.get("published_at") or a.get("published_utc") or "")[:10]
            if title:
                lines.append(f"• [{published}] {source}: {title}")
                if summary:
                    lines.append(f"  {summary[:200]}")

        return "\n".join(lines)

    except Exception as exc:
        log.warning("[%s] Robinhood news failed: %s", ticker, exc)
        return ""


def get_robinhood_market_news() -> str:
    """Pull general market news from Robinhood."""
    try:
        rh = _rh()
        result = rh._call("get_market_news", {"limit": 15}) or {}
        articles = result.get("results", result.get("news", []))

        if not articles:
            return ""

        lines = ["[Robinhood Market News]"]
        for a in articles[:10]:
            title = a.get("title") or a.get("headline") or ""
            source = a.get("source") or ""
            published = (a.get("published_at") or "")[:10]
            if title:
                lines.append(f"• [{published}] {source}: {title}")

        return "\n".join(lines)

    except Exception as exc:
        log.warning("Robinhood market news failed: %s", exc)
        return ""


# ── Main scanner ──────────────────────────────────────────────────────────────

def scan_market() -> List[Tuple[str, str]]:
    """Return top (ticker, asset_type) pairs for this cycle from Robinhood."""
    results: List[Tuple[str, str]] = []

    results.extend(_scan_stocks())
    results.extend(_scan_crypto())
    results.extend(_scan_options())
    results.extend(_scan_predictions())

    # Deduplicate keeping first occurrence
    seen, unique = set(), []
    for item in results:
        if item[0] not in seen:
            seen.add(item[0])
            unique.append(item)

    final = unique[:settings.scanner_max_tickers]
    log.info("Scanner picks: %s", final)
    return final


def get_ticker_context(ticker: str) -> Dict:
    """Pull current price + daily stats from Robinhood."""
    try:
        rh = _rh()
        return rh.get_market_context(ticker, "stock")
    except Exception as exc:
        log.warning("[%s] get_ticker_context failed: %s", ticker, exc)
        return {}
