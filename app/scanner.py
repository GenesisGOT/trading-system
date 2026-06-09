"""Market scanner — finds top opportunities across stocks, crypto, and options.

Each cycle returns a list of (ticker, asset_type) pairs for the graph to analyze.

Sources:
  Stocks  — Polygon.io top gainers/losers/volume (free tier)
  Crypto  — Polygon.io crypto snapshots + fixed watchlist
  Options — Unusual options activity via Tavily search
  Prediction — fixed watchlist (Robinhood prediction markets)
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import httpx

from app.config import settings

log = logging.getLogger(__name__)

POLYGON_BASE = "https://api.polygon.io"

MIN_PRICE   = 5.0
MIN_VOLUME  = 500_000

# Always-on crypto watchlist (Robinhood supports these)
CRYPTO_WATCHLIST = ["BTC", "ETH", "SOL", "DOGE", "AVAX", "MATIC", "LINK", "UNI"]

# Prediction market contracts on Robinhood (event-based)
PREDICTION_WATCHLIST: List[str] = []  # populate from .env PREDICTION_SYMBOLS


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {settings.polygon_api_key}"}


def _get(path: str, params: dict | None = None) -> Optional[dict]:
    try:
        r = httpx.get(
            f"{POLYGON_BASE}{path}",
            headers=_headers(),
            params=params or {},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("Polygon request failed %s: %s", path, exc)
        return None


def _snapshot_movers(direction: str) -> List[dict]:
    data = _get(f"/v2/snapshot/locale/us/markets/stocks/{direction}")
    return (data or {}).get("tickers", [])


def _score(snap: dict) -> float:
    day  = snap.get("day", {})
    prev = snap.get("prevDay", {})
    price      = day.get("c", 0) or 0
    volume     = day.get("v", 0) or 0
    prev_close = prev.get("c", 1) or 1
    change_pct = abs((price - prev_close) / prev_close * 100) if prev_close else 0
    return (min(volume / 1_000_000, 10) * 0.4) + (min(change_pct, 20) * 0.6)


def _scan_stocks() -> List[Tuple[str, str]]:
    """Return top stock movers as (ticker, 'stock') pairs."""
    if not settings.polygon_api_key:
        return [(s, "stock") for s in settings.allowed_symbols]

    gainers = _snapshot_movers("gainers")
    losers  = _snapshot_movers("losers")
    candidates = {t["ticker"]: t for t in gainers + losers if t.get("ticker")}

    filtered = []
    for ticker, snap in candidates.items():
        day = snap.get("day", {})
        if (day.get("c", 0) or 0) < MIN_PRICE:
            continue
        if (day.get("v", 0) or 0) < MIN_VOLUME:
            continue
        filtered.append((ticker, _score(snap)))

    filtered.sort(key=lambda x: x[1], reverse=True)
    max_stocks = max(1, settings.scanner_max_tickers - 2)  # leave room for crypto
    top = [(t, "stock") for t, _ in filtered[:max_stocks]]
    return top or [(s, "stock") for s in settings.allowed_symbols]


def _scan_crypto() -> List[Tuple[str, str]]:
    """Return active crypto symbols as (ticker, 'crypto') pairs."""
    enabled = [s.strip().upper() for s in settings.scanner_crypto_symbols.split(",") if s.strip()]
    if not enabled:
        return []

    # Always include top 2 from watchlist that are enabled
    result = [(s, "crypto") for s in enabled[:2]]
    return result


def _scan_options() -> List[Tuple[str, str]]:
    """Find unusual options activity via Tavily. Returns (ticker, 'option') pairs."""
    if not (settings.scanner_options_enabled and settings.tavily_api_key):
        return []
    try:
        from app.agents.tools.search import search_ticker_news
        from tavily import TavilyClient
        client = TavilyClient(api_key=settings.tavily_api_key)
        result = client.search(
            query="unusual options activity today high volume calls puts sweep",
            search_depth="basic",
            max_results=3,
            include_answer=True,
        )
        # Extract tickers from answer — simple heuristic
        import re
        text = result.get("answer", "") + " ".join(r.get("content", "") for r in result.get("results", []))
        tickers = list(dict.fromkeys(re.findall(r'\b([A-Z]{2,5})\b', text)))
        # Filter to known symbols (avoid false positives)
        valid = [t for t in tickers if len(t) >= 2 and t not in
                 {"THE", "FOR", "AND", "BUT", "NOT", "ARE", "WAS", "HAS", "ITS", "NEW", "ALL",
                  "USD", "ETF", "IPO", "CEO", "CFO", "SEC", "FDA", "EPS", "YOY", "QOQ"}]
        return [(t, "option") for t in valid[:2]]
    except Exception as exc:
        log.warning("Options scanner failed: %s", exc)
        return []


def _scan_predictions() -> List[Tuple[str, str]]:
    """Return prediction market contracts from config."""
    symbols = [s.strip() for s in settings.prediction_symbols.split(",") if s.strip()]
    return [(s, "prediction") for s in symbols]


def scan_market() -> List[Tuple[str, str]]:
    """Return top (ticker, asset_type) pairs for this cycle.

    Total capped at scanner_max_tickers. Mix: stocks + crypto + options + predictions.
    """
    results: List[Tuple[str, str]] = []

    results.extend(_scan_stocks())
    results.extend(_scan_crypto())
    results.extend(_scan_options())
    results.extend(_scan_predictions())

    # Deduplicate keeping first occurrence
    seen = set()
    unique = []
    for item in results:
        if item[0] not in seen:
            seen.add(item[0])
            unique.append(item)

    final = unique[:settings.scanner_max_tickers]
    log.info("Scanner picks: %s", [(t, a) for t, a in final])
    return final


def get_ticker_context(ticker: str) -> Dict:
    """Pull current price + daily stats for a stock ticker."""
    if not settings.polygon_api_key:
        return {}
    try:
        r = httpx.get(
            f"{POLYGON_BASE}/v2/snapshot/locale/us/markets/stocks/tickers",
            headers=_headers(),
            params={"tickers": ticker, "include_otc": "false"},
            timeout=15,
        )
        r.raise_for_status()
        snaps = r.json().get("tickers", [])
        if not snaps:
            return {}
        s    = snaps[0]
        day  = s.get("day", {})
        prev = s.get("prevDay", {})
        price      = day.get("c", 0)
        prev_close = prev.get("c", 0)
        change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
        return {
            "price":      price,
            "change_pct": round(change_pct, 2),
            "volume":     day.get("v", 0),
            "high":       day.get("h", 0),
            "low":        day.get("l", 0),
        }
    except Exception as exc:
        log.warning("[%s] get_ticker_context failed: %s", ticker, exc)
        return {}
