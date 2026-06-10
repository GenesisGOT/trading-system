"""Earnings calendar — warns agents when earnings are approaching."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

log = logging.getLogger(__name__)


def get_earnings_context(ticker: str) -> str:
    """Return earnings date context for a ticker. Agents use this to adjust risk."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).calendar
        if info is None or info.empty:
            return ""

        # yfinance calendar returns a DataFrame with earnings date
        earnings_date = None
        if "Earnings Date" in info.index:
            val = info.loc["Earnings Date"]
            if hasattr(val, "iloc"):
                earnings_date = val.iloc[0]
            else:
                earnings_date = val

        if not earnings_date:
            return ""

        today = date.today()
        try:
            ed = earnings_date.date() if hasattr(earnings_date, "date") else earnings_date
        except Exception:
            return ""

        days_away = (ed - today).days

        if days_away < 0:
            return f"⚠️ EARNINGS: Reported {abs(days_away)} days ago"
        if days_away == 0:
            return "🚨 EARNINGS TODAY — extreme caution, high volatility expected"
        if days_away <= 3:
            return f"🚨 EARNINGS IN {days_away} DAY(S) — high event risk, consider smaller position"
        if days_away <= 7:
            return f"⚠️ EARNINGS IN {days_away} DAYS — elevated risk, options IV may be inflated"
        if days_away <= 14:
            return f"📅 EARNINGS IN {days_away} DAYS — monitor for pre-earnings drift"
        return ""
    except Exception as exc:
        log.debug("[%s] Earnings lookup failed: %s", ticker, exc)
        return ""
