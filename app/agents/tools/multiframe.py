"""Multi-timeframe analysis — confirms trend alignment across 1h, 4h, and daily charts.

A trade is only taken when at least 2 of 3 timeframes agree on direction.
Returns a formatted string for agents and a numeric confluence score (-1 to +1).
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)

TIMEFRAMES = {
    "1h":    ("1h",  "30d"),
    "4h":    ("1h",  "60d"),   # yfinance max 1h interval is 60d; we resample to 4h
    "daily": ("1d",  "90d"),
}


def _ema_trend(close: pd.Series) -> int:
    """Return +1 (uptrend), -1 (downtrend), 0 (mixed) based on EMA alignment."""
    if len(close) < 50:
        return 0
    ema9  = close.ewm(span=9).mean().iloc[-1]
    ema21 = close.ewm(span=21).mean().iloc[-1]
    ema50 = close.ewm(span=50).mean().iloc[-1]
    if ema9 > ema21 > ema50:
        return 1
    if ema9 < ema21 < ema50:
        return -1
    return 0


def _rsi(close: pd.Series) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, float("nan"))
    return float((100 - 100 / (1 + rs)).iloc[-1])


def _macd_bias(close: pd.Series) -> int:
    """Return +1 if MACD > signal, -1 otherwise."""
    macd   = close.ewm(span=12).mean() - close.ewm(span=26).mean()
    signal = macd.ewm(span=9).mean()
    return 1 if macd.iloc[-1] > signal.iloc[-1] else -1


def _analyze_tf(df: pd.DataFrame) -> Tuple[int, float, str]:
    """Returns (trend: -1/0/1, rsi, label)."""
    if df.empty or len(df) < 20:
        return 0, 50.0, "insufficient data"
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"].squeeze()
    trend = _ema_trend(close)
    rsi   = _rsi(close)
    macd_b = _macd_bias(close)
    combined = trend + macd_b  # -2 to +2
    label_map = {2: "STRONG BULL", 1: "BULL", 0: "NEUTRAL", -1: "BEAR", -2: "STRONG BEAR"}
    return trend, rsi, label_map.get(combined, "NEUTRAL")


def get_multiframe_analysis(ticker: str, asset_type: str = "stock") -> Tuple[str, float]:
    """Return (formatted string for agents, confluence score -1 to +1).

    Confluence score:
      +1.0 = all 3 timeframes bullish
      -1.0 = all 3 timeframes bearish
       0.0 = mixed / no edge
    """
    try:
        import yfinance as yf
        symbol = f"{ticker}-USD" if asset_type == "crypto" and not ticker.endswith("-USD") else ticker

        results = {}

        # Daily
        df_d = yf.download(symbol, period="90d", interval="1d", progress=False, auto_adjust=True)
        results["daily"] = _analyze_tf(df_d)

        # 1h
        df_1h = yf.download(symbol, period="7d", interval="1h", progress=False, auto_adjust=True)
        results["1h"] = _analyze_tf(df_1h)

        # 4h — resample from 1h data
        if not df_1h.empty:
            if isinstance(df_1h.columns, pd.MultiIndex):
                df_1h.columns = df_1h.columns.get_level_values(0)
            df_4h = df_1h.resample("4h").agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum"
            }).dropna()
            results["4h"] = _analyze_tf(df_4h)
        else:
            results["4h"] = (0, 50.0, "no data")

        # Compute confluence
        trends = [results[tf][0] for tf in ["daily", "4h", "1h"]]
        bull = sum(1 for t in trends if t > 0)
        bear = sum(1 for t in trends if t < 0)
        confluence = (bull - bear) / 3.0  # -1 to +1

        # Format output
        lines = [f"MULTI-TIMEFRAME ANALYSIS ({ticker}):"]
        tf_labels = {"daily": "Daily", "4h": "4-Hour", "1h": "1-Hour"}
        for tf_key, (trend, rsi, label) in results.items():
            arrow = "↑" if trend > 0 else "↓" if trend < 0 else "→"
            lines.append(f"  {tf_labels[tf_key]}: {label} {arrow}  RSI={rsi:.0f}")

        align_str = "ALIGNED BULLISH ✅" if confluence >= 0.67 else \
                    "ALIGNED BEARISH ❌" if confluence <= -0.67 else \
                    "MIXED — confirm before trading ⚠️"
        lines.append(f"  Confluence: {align_str} (score={confluence:+.2f})")

        if confluence >= 0.67:
            lines.append("  → All/most timeframes agree UP — higher conviction BUY")
        elif confluence <= -0.67:
            lines.append("  → All/most timeframes agree DOWN — avoid BUY, consider SELL")
        else:
            lines.append("  → Timeframes conflict — reduce position size or wait for alignment")

        return "\n".join(lines), confluence

    except Exception as exc:
        log.warning("[%s] Multi-timeframe analysis failed: %s", ticker, exc)
        return "", 0.0
