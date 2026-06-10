"""Unusual Whales options flow — large sweeps and dark pool prints.

Unusual Whales tracks every options trade and flags:
  - Large sweep orders (market orders that sweep multiple exchanges)
  - Dark pool prints (off-exchange block trades)
  - Unusual volume relative to open interest

Requires UNUSUAL_WHALES_API_KEY (free tier available at unusualwhales.com).
Falls back to Tavily search if no key is configured.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)

UW_BASE = "https://api.unusualwhales.com/api"


def _headers() -> dict:
    key = getattr(settings, "unusual_whales_api_key", None)
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def get_options_flow(ticker: str) -> str:
    """Return recent unusual options activity for ticker."""
    key = getattr(settings, "unusual_whales_api_key", None)
    if key:
        result = _uw_api_flow(ticker, key)
        if result:
            return result
    return _tavily_fallback(ticker)


def _uw_api_flow(ticker: str, key: str) -> str:
    """Fetch from Unusual Whales REST API."""
    try:
        resp = httpx.get(
            f"{UW_BASE}/stock/{ticker}/option-contracts/flow",
            headers=_headers(),
            params={"limit": 20},
            timeout=10,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json().get("data", [])
        if not data:
            return ""

        lines = [f"UNUSUAL WHALES OPTIONS FLOW ({ticker}):"]
        calls, puts = 0, 0
        call_premium, put_premium = 0, 0
        sweeps = []

        for item in data[:15]:
            side      = item.get("put_call", "").upper()
            premium   = item.get("total_premium", 0) or 0
            is_sweep  = item.get("is_sweep", False)
            sentiment = item.get("sentiment", "")
            expiry    = item.get("expiry", "")
            strike    = item.get("strike", "")
            iv        = item.get("implied_volatility", 0) or 0

            if side == "CALL":
                calls += 1
                call_premium += premium
            elif side == "PUT":
                puts += 1
                put_premium += premium

            if is_sweep and premium > 50_000:
                sweeps.append(
                    f"  🌊 SWEEP {side} ${strike} exp={expiry} "
                    f"premium=${premium:,.0f} IV={iv:.0%} [{sentiment}]"
                )

        total = calls + puts
        if total:
            bull_pct = calls / total * 100
            lines.append(
                f"  Calls: {calls} (${call_premium:,.0f}) | Puts: {puts} (${put_premium:,.0f})"
            )
            lines.append(f"  Flow bias: {'BULLISH' if bull_pct > 60 else 'BEARISH' if bull_pct < 40 else 'NEUTRAL'} ({bull_pct:.0f}% calls)")

        if sweeps:
            lines.append("  Large Sweeps:")
            lines.extend(sweeps[:5])

        return "\n".join(lines)
    except Exception as exc:
        log.debug("[%s] Unusual Whales API failed: %s", ticker, exc)
        return ""


def _tavily_fallback(ticker: str) -> str:
    """Use Tavily to search for unusual options activity when no API key."""
    try:
        from app.agents.tools.search import search_ticker_news
        return search_ticker_news(
            ticker,
            f"{ticker} unusual options activity sweep dark pool flow today",
        )
    except Exception:
        return ""


def get_dark_pool_prints(ticker: str) -> str:
    """Return recent dark pool (off-exchange) block trades."""
    key = getattr(settings, "unusual_whales_api_key", None)
    if not key:
        return ""
    try:
        resp = httpx.get(
            f"{UW_BASE}/darkpool/{ticker}/recent",
            headers=_headers(),
            params={"limit": 10},
            timeout=10,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json().get("data", [])
        if not data:
            return ""

        lines = [f"DARK POOL PRINTS ({ticker}):"]
        total_vol = 0
        for item in data[:5]:
            price = item.get("price", 0)
            size  = item.get("size", 0)
            value = price * size
            total_vol += value
            lines.append(f"  ${price:.2f} × {size:,} = ${value:,.0f}")
        lines.append(f"  Total dark pool volume: ${total_vol:,.0f}")
        return "\n".join(lines)
    except Exception as exc:
        log.debug("[%s] Dark pool fetch failed: %s", ticker, exc)
        return ""
