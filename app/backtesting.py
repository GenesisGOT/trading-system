"""VectorBT backtesting wrapper — validate signal logic on historical data.

Exposes a simple API: given a ticker and signal function, run it over
historical OHLCV data and return performance stats.

Used by the /backtest API endpoint and optionally by the Risk Manager agent
before deploying a new strategy to live trading.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    ticker: str
    period: str
    total_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    total_trades: int
    avg_trade_return: float
    best_trade: float
    worst_trade: float
    summary: str


def run_backtest(
    ticker: str,
    period: str = "1y",
    asset_type: str = "stock",
    initial_capital: float = 10_000.0,
) -> Optional[BacktestResult]:
    """Run a backtest of our standard signal logic (EMA crossover + RSI filter)
    on historical data and return performance statistics.

    Args:
        ticker: Symbol to test
        period: yfinance period string e.g. "1y", "2y", "6mo"
        asset_type: stock | crypto
        initial_capital: Starting capital in USD
    """
    try:
        import vectorbt as vbt
        import numpy as np
        import yfinance as yf

        symbol = f"{ticker}-USD" if asset_type == "crypto" and not ticker.endswith("-USD") else ticker
        df = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=True)

        if df.empty or len(df) < 50:
            return _pandas_backtest(ticker, period, asset_type, initial_capital)

        if hasattr(df.columns, "get_level_values"):
            df.columns = df.columns.get_level_values(0)

        close = df["Close"]

        # ── Signal: EMA(9) > EMA(21) AND RSI < 70 = BUY; EMA(9) < EMA(21) = SELL ──
        ema9  = close.ewm(span=9).mean()
        ema21 = close.ewm(span=21).mean()

        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))

        entries = (ema9 > ema21) & (rsi < 70) & (ema9.shift(1) <= ema21.shift(1))
        exits   = (ema9 < ema21) & (ema9.shift(1) >= ema21.shift(1))

        pf = vbt.Portfolio.from_signals(
            close,
            entries,
            exits,
            init_cash=initial_capital,
            fees=0.001,      # 0.1% commission
            slippage=0.001,  # 0.1% slippage
        )

        stats = pf.stats()
        trades = pf.trades.records_readable

        total_return = float(stats.get("Total Return [%]", 0)) / 100
        sharpe       = float(stats.get("Sharpe Ratio", 0))
        max_dd       = float(stats.get("Max Drawdown [%]", 0)) / 100
        n_trades     = int(stats.get("Total Trades", 0))
        win_rate     = float(stats.get("Win Rate [%]", 0)) / 100

        avg_ret = 0.0
        best    = 0.0
        worst   = 0.0
        if not trades.empty and "Return" in trades.columns:
            avg_ret = float(trades["Return"].mean())
            best    = float(trades["Return"].max())
            worst   = float(trades["Return"].min())

        summary = (
            f"{ticker} backtest ({period}): Return={total_return:+.1%} | "
            f"Sharpe={sharpe:.2f} | MaxDD={max_dd:.1%} | "
            f"WinRate={win_rate:.0%} | Trades={n_trades}"
        )
        log.info(summary)

        return BacktestResult(
            ticker=ticker,
            period=period,
            total_return=total_return,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            win_rate=win_rate,
            total_trades=n_trades,
            avg_trade_return=avg_ret,
            best_trade=best,
            worst_trade=worst,
            summary=summary,
        )

    except ImportError:
        log.info("vectorbt not installed — using pandas backtest")
        return _pandas_backtest(ticker, period, asset_type, initial_capital)
    except Exception as exc:
        log.error("[%s] Backtest failed: %s", ticker, exc)
        return None


def _pandas_backtest(
    ticker: str,
    period: str,
    asset_type: str,
    initial_capital: float,
) -> Optional[BacktestResult]:
    """Pure-pandas fallback backtest when vectorbt is unavailable."""
    try:
        import numpy as np
        import yfinance as yf

        symbol = f"{ticker}-USD" if asset_type == "crypto" and not ticker.endswith("-USD") else ticker
        df = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 50:
            return None

        if hasattr(df.columns, "get_level_values"):
            df.columns = df.columns.get_level_values(0)

        close = df["Close"].squeeze()
        ema9  = close.ewm(span=9).mean()
        ema21 = close.ewm(span=21).mean()

        signal = (ema9 > ema21).astype(int)
        returns = close.pct_change()
        strat_returns = signal.shift(1) * returns

        total_return = float((1 + strat_returns).prod() - 1)
        sharpe = float(strat_returns.mean() / strat_returns.std() * (252 ** 0.5)) if strat_returns.std() > 0 else 0
        equity = (1 + strat_returns).cumprod()
        max_dd = float((equity / equity.cummax() - 1).min())

        trades = signal.diff().abs()
        n_trades = int(trades.sum())

        trade_returns = []
        in_trade = False
        entry_price = 0.0
        for i, (price, sig) in enumerate(zip(close, signal)):
            if sig == 1 and not in_trade:
                in_trade = True
                entry_price = price
            elif sig == 0 and in_trade:
                in_trade = False
                trade_returns.append((price - entry_price) / entry_price)

        win_rate = sum(1 for r in trade_returns if r > 0) / len(trade_returns) if trade_returns else 0.5
        avg_ret  = float(np.mean(trade_returns)) if trade_returns else 0.0
        best     = float(max(trade_returns)) if trade_returns else 0.0
        worst    = float(min(trade_returns)) if trade_returns else 0.0

        summary = (
            f"{ticker} backtest ({period}): Return={total_return:+.1%} | "
            f"Sharpe={sharpe:.2f} | MaxDD={max_dd:.1%} | "
            f"WinRate={win_rate:.0%} | Trades={n_trades}"
        )

        return BacktestResult(
            ticker=ticker, period=period,
            total_return=total_return, sharpe_ratio=sharpe,
            max_drawdown=max_dd, win_rate=win_rate,
            total_trades=n_trades, avg_trade_return=avg_ret,
            best_trade=best, worst_trade=worst, summary=summary,
        )
    except Exception as exc:
        log.error("[%s] Pandas backtest failed: %s", ticker, exc)
        return None
