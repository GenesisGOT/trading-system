"""Kelly Criterion position sizing — scales order size by confidence and win rate.

Kelly formula: f = (bp - q) / b
  b = odds (expected gain / expected loss ratio)
  p = win probability (estimated from confidence score)
  q = 1 - p

Uses historical win rate from the trade database to anchor the estimate.
Applies a fractional Kelly (0.25x) to reduce variance — standard practice.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

KELLY_FRACTION = 0.25   # fractional Kelly — reduces bet size to lower variance
MIN_ORDER_USD  = 10.0   # never bet less than $10
MAX_KELLY_PCT  = 0.20   # never risk more than 20% of buying power on one trade


def _historical_win_rate(ticker: str) -> float:
    """Pull win rate from trade history. Falls back to 0.5 if no data."""
    try:
        from app.database import get_db
        with get_db() as conn:
            rows = conn.execute("""
                SELECT te.side, te.notional_usd,
                       LAG(te.notional_usd) OVER (PARTITION BY te.ticker ORDER BY te.created_at) as prev_notional
                FROM trade_executions te
                WHERE te.ticker = ? AND te.status IN ('submitted', 'dry_run')
                ORDER BY te.created_at DESC
                LIMIT 30
            """, (ticker,)).fetchall()
            if len(rows) < 5:
                return 0.5  # not enough history
            buys  = [r for r in rows if r["side"] == "buy"]
            sells = [r for r in rows if r["side"] == "sell"]
            if not buys:
                return 0.5
            wins = min(len(sells), len(buys))
            return wins / len(buys) if buys else 0.5
    except Exception:
        return 0.5


def _portfolio_win_rate() -> float:
    """Overall win rate across all tickers."""
    try:
        from app.database import get_db
        with get_db() as conn:
            row = conn.execute("""
                SELECT
                    SUM(CASE WHEN side='sell' AND status='submitted' THEN 1 ELSE 0 END) as exits,
                    SUM(CASE WHEN side='buy'  AND status='submitted' THEN 1 ELSE 0 END) as entries
                FROM trade_executions
                WHERE created_at >= datetime('now', '-30 days')
            """).fetchone()
            if not row or not row["entries"] or row["entries"] < 5:
                return 0.5
            return min(row["exits"], row["entries"]) / row["entries"]
    except Exception:
        return 0.5


def kelly_size(
    confidence: float,
    buying_power: float,
    ticker: str,
    asset_type: str = "stock",
    win_rate_override: Optional[float] = None,
) -> float:
    """Return recommended notional USD for this trade using fractional Kelly.

    Args:
        confidence: Agent confidence score 0.0-1.0
        buying_power: Available cash from broker
        ticker: Symbol (used to fetch historical win rate)
        asset_type: stock | crypto | option | prediction
        win_rate_override: Skip DB lookup and use this win rate directly

    Returns:
        Notional USD to deploy (capped at mandate_max_order_usd and MAX_KELLY_PCT)
    """
    if buying_power <= 0:
        return settings.mandate_max_order_usd

    win_rate = win_rate_override or _portfolio_win_rate()
    # Blend ticker-specific and portfolio win rate
    ticker_wr = _historical_win_rate(ticker)
    blended_wr = 0.6 * ticker_wr + 0.4 * win_rate

    # Adjust win rate by agent confidence (confidence acts as a signal quality multiplier)
    p = max(0.35, min(0.80, blended_wr * (0.7 + 0.6 * confidence)))
    q = 1 - p

    # Expected gain/loss ratio — higher for higher-confidence trades
    # base 2:1 reward:risk, scaled by confidence
    b = 1.5 + confidence * 1.0   # ranges from 1.5 to 2.5

    # Kelly fraction
    kelly_f = (b * p - q) / b
    kelly_f = max(0.0, kelly_f)  # never negative

    # Apply fractional Kelly and cap
    fractional = kelly_f * KELLY_FRACTION
    fractional = min(fractional, MAX_KELLY_PCT)

    # Crypto is more volatile — reduce further
    if asset_type == "crypto":
        fractional *= 0.6

    # Apply market regime multiplier
    try:
        from app.agents.tools.regime import get_market_regime
        regime = get_market_regime()
        fractional *= regime.kelly_mult
        if regime.kelly_mult < 1.0:
            log.info("[%s] Regime=%s applying %.1fx Kelly multiplier", ticker, regime.regime, regime.kelly_mult)
    except Exception:
        pass

    notional = round(buying_power * fractional, 2)
    notional = max(notional, MIN_ORDER_USD)

    # Hard cap from mandate
    cap = settings.mandate_max_order_usd
    if cap < 999_999:
        notional = min(notional, cap)

    log.info(
        "[%s] Kelly sizing: conf=%.2f win_rate=%.2f kelly_f=%.3f fractional=%.3f → $%.2f",
        ticker, confidence, blended_wr, kelly_f, fractional, notional,
    )
    return notional
