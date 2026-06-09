"""Market scanner — uses Polygon.io free tier to find the best trading opportunities
across the entire US equity market each cycle instead of fixed tickers.

Strategy:
  1. Pull top gainers + top losers (price momentum)
  2. Pull highest unusual-volume tickers (volume surge)
  3. Score and rank each candidate
  4. Return top N tickers for TradingAgents to analyze
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)

POLYGON_BASE = "https://api.polygon.io"

# Minimum filters to keep noise out
MIN_PRICE = 5.0          # skip penny stocks
MIN_VOLUME = 500_000     # skip illiquid names
MAX_TICKERS = 10         # how many to pass to TradingAgents each cycle


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
    """Get top gainers or losers snapshots."""
    data = _get(f"/v2/snapshot/locale/us/markets/stocks/{direction}")
    if not data:
        return []
    return data.get("tickers", [])


def _snapshot_tickers(tickers: List[str]) -> List[dict]:
    """Get snapshots for a specific list of tickers."""
    if not tickers:
        return []
    data = _get(
        "/v2/snapshot/locale/us/markets/stocks/tickers",
        {"tickers": ",".join(tickers), "include_otc": "false"},
    )
    if not data:
        return []
    return data.get("tickers", [])


def _score(ticker_snapshot: dict) -> float:
    """Score a ticker on momentum + volume + price movement."""
    day = ticker_snapshot.get("day", {})
    prev = ticker_snapshot.get("prevDay", {})

    price = day.get("c", 0) or 0
    volume = day.get("v", 0) or 0
    prev_close = prev.get("c", 1) or 1
    change_pct = abs((price - prev_close) / prev_close * 100) if prev_close else 0

    # Weighted score: volume surge + price movement
    vol_score = min(volume / 1_000_000, 10)   # cap at 10M shares
    move_score = min(change_pct, 20)            # cap at 20%
    return (vol_score * 0.4) + (move_score * 0.6)


def scan_market() -> List[str]:
    """Return top tickers for this cycle.

    Falls back to settings.allowed_symbols if Polygon key is missing or API fails.
    """
    if not settings.polygon_api_key:
        log.info("No POLYGON_API_KEY — using fixed symbol list: %s", settings.allowed_symbols)
        return settings.allowed_symbols

    log.info("Scanning market via Polygon.io...")

    gainers = _snapshot_movers("gainers")
    losers = _snapshot_movers("losers")

    candidates = {t["ticker"]: t for t in gainers + losers if t.get("ticker")}

    # Filter
    filtered = []
    for ticker, snap in candidates.items():
        day = snap.get("day", {})
        price = day.get("c", 0) or 0
        volume = day.get("v", 0) or 0
        if price < MIN_PRICE or volume < MIN_VOLUME:
            continue
        filtered.append((ticker, _score(snap)))

    # Sort by score, take top N
    filtered.sort(key=lambda x: x[1], reverse=True)
    top = [t for t, _ in filtered[:MAX_TICKERS]]

    if not top:
        log.warning("Scanner returned no results — falling back to fixed symbols")
        return settings.allowed_symbols

    log.info("Scanner top picks: %s", top)
    return top


def get_ticker_context(ticker: str) -> Dict:
    """Pull current price + daily stats for a ticker (used to enrich notifications)."""
    snaps = _snapshot_tickers([ticker])
    if not snaps:
        return {}
    s = snaps[0]
    day = s.get("day", {})
    prev = s.get("prevDay", {})
    price = day.get("c", 0)
    prev_close = prev.get("c", 0)
    change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
    return {
        "price": price,
        "change_pct": round(change_pct, 2),
        "volume": day.get("v", 0),
        "high": day.get("h", 0),
        "low": day.get("l", 0),
    }
