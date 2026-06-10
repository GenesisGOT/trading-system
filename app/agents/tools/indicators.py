"""Technical indicators — feeds VWAP, RSI, MACD, Bollinger Bands, RVOL to agents.

Uses yfinance (already installed) + pandas-ta for computation.
Falls back gracefully if data unavailable.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import pandas as pd

log = logging.getLogger(__name__)


def get_indicators(ticker: str, asset_type: str = "stock") -> str:
    """Return a formatted string of key technical indicators for the agents to read."""
    try:
        if asset_type == "crypto":
            return _crypto_indicators(ticker)
        return _stock_indicators(ticker)
    except Exception as exc:
        log.warning("[%s] Indicators failed: %s", ticker, exc)
        return ""


def _stock_indicators(ticker: str) -> str:
    try:
        import yfinance as yf
        df = yf.download(ticker, period="60d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            return ""
        return _compute(ticker, df)
    except Exception as exc:
        log.warning("[%s] Stock indicators failed: %s", ticker, exc)
        return ""


def _crypto_indicators(ticker: str) -> str:
    try:
        import yfinance as yf
        symbol = f"{ticker}-USD" if not ticker.endswith("-USD") else ticker
        df = yf.download(symbol, period="60d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            return ""
        return _compute(ticker, df)
    except Exception as exc:
        log.warning("[%s] Crypto indicators failed: %s", ticker, exc)
        return ""


def _compute(ticker: str, df: pd.DataFrame) -> str:
    try:
        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close = df["Close"]
        high  = df["High"]
        low   = df["Low"]
        vol   = df["Volume"]

        # ── RSI ────────────────────────────────────────────────────────────
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi   = (100 - 100 / (1 + rs)).iloc[-1]

        # ── MACD ───────────────────────────────────────────────────────────
        ema12  = close.ewm(span=12).mean()
        ema26  = close.ewm(span=26).mean()
        macd   = (ema12 - ema26).iloc[-1]
        signal = (ema12 - ema26).ewm(span=9).mean().iloc[-1]
        macd_cross = "BULLISH crossover" if macd > signal else "BEARISH crossover"

        # ── Bollinger Bands ────────────────────────────────────────────────
        sma20  = close.rolling(20).mean()
        std20  = close.rolling(20).std()
        bb_upper = (sma20 + 2 * std20).iloc[-1]
        bb_lower = (sma20 - 2 * std20).iloc[-1]
        bb_mid   = sma20.iloc[-1]
        price    = close.iloc[-1]
        bb_pos   = "ABOVE upper band (overbought)" if price > bb_upper else \
                   "BELOW lower band (oversold)" if price < bb_lower else \
                   "inside bands"

        # ── EMAs ───────────────────────────────────────────────────────────
        ema9  = close.ewm(span=9).mean().iloc[-1]
        ema21 = close.ewm(span=21).mean().iloc[-1]
        ema50 = close.ewm(span=50).mean().iloc[-1]
        trend = "UPTREND" if ema9 > ema21 > ema50 else \
                "DOWNTREND" if ema9 < ema21 < ema50 else "MIXED"

        # ── Relative Volume (RVOL) ─────────────────────────────────────────
        avg_vol  = vol.rolling(20).mean().iloc[-2]  # yesterday's 20d avg
        today_vol = vol.iloc[-1]
        rvol = today_vol / avg_vol if avg_vol else 1.0

        # ── ATR (volatility for stop sizing) ──────────────────────────────
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        atr_pct = (atr / price * 100) if price else 0

        return f"""TECHNICAL INDICATORS ({ticker}):
  Price: ${price:.2f}
  RSI(14): {rsi:.1f} {'— OVERBOUGHT' if rsi > 70 else '— OVERSOLD' if rsi < 30 else '— neutral'}
  MACD: {macd:.3f} vs Signal {signal:.3f} — {macd_cross}
  Bollinger Bands: {bb_pos} (upper ${bb_upper:.2f} / mid ${bb_mid:.2f} / lower ${bb_lower:.2f})
  EMA Trend: {trend} (9: ${ema9:.2f} / 21: ${ema21:.2f} / 50: ${ema50:.2f})
  Relative Volume: {rvol:.1f}x average {'— HIGH ACTIVITY' if rvol > 2 else ''}
  ATR(14): ${atr:.2f} ({atr_pct:.1f}% of price) — suggested stop: ${price - 1.5*atr:.2f}"""
    except Exception as exc:
        log.warning("[%s] Indicator compute failed: %s", ticker, exc)
        return ""


def suggested_stop_loss(ticker: str, entry_price: float, asset_type: str = "stock") -> Optional[float]:
    """Return a suggested stop loss price based on 1.5x ATR below entry."""
    try:
        import yfinance as yf
        symbol = f"{ticker}-USD" if asset_type == "crypto" and not ticker.endswith("-USD") else ticker
        df = yf.download(symbol, period="30d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 14:
            return round(entry_price * 0.95, 2)  # fallback: 5%

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        tr = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - df["Close"].shift()).abs(),
            (df["Low"] - df["Close"].shift()).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        stop = round(entry_price - 1.5 * atr, 2)
        return max(stop, entry_price * 0.90)  # never more than 10% loss
    except Exception:
        return round(entry_price * 0.95, 2)
