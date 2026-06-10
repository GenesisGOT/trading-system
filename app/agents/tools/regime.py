"""Market regime detection — classifies market as bull / bear / neutral / crash.

Uses VIX level + SPY rolling returns + Hidden Markov Model (hmmlearn).
Result feeds into Kelly sizer to scale position sizes by regime.

Regime map:
  bull    → full Kelly, normal operation
  neutral → 0.7x Kelly
  bear    → 0.4x Kelly, tighten trailing stops
  crash   → 0.1x Kelly, near-halt (preserve capital)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

REGIME_KELLY_MULTIPLIERS = {
    "bull":    1.0,
    "neutral": 0.7,
    "bear":    0.4,
    "crash":   0.1,
}


@dataclass
class RegimeResult:
    regime: str          # bull | neutral | bear | crash
    vix: float
    spy_return_5d: float
    confidence: float    # HMM state probability
    kelly_mult: float
    description: str


_cached_regime: Optional[RegimeResult] = None
_cache_ts: float = 0.0
_CACHE_TTL = 3600.0  # refresh hourly


def get_market_regime() -> RegimeResult:
    """Return current market regime. Cached for 1 hour."""
    import time
    global _cached_regime, _cache_ts

    now = time.time()
    if _cached_regime and (now - _cache_ts) < _CACHE_TTL:
        return _cached_regime

    result = _detect_regime()
    _cached_regime = result
    _cache_ts = now
    return result


def _detect_regime() -> RegimeResult:
    try:
        return _hmm_regime()
    except Exception as exc:
        log.warning("HMM regime detection failed (%s) — falling back to VIX heuristic", exc)
        return _vix_heuristic()


def _hmm_regime() -> RegimeResult:
    import numpy as np
    import yfinance as yf
    from hmmlearn import hmm

    # Pull SPY + VIX
    spy = yf.download("SPY", period="2y", interval="1d", progress=False, auto_adjust=True)
    vix_df = yf.download("^VIX", period="2y", interval="1d", progress=False, auto_adjust=True)

    if spy.empty or vix_df.empty:
        return _vix_heuristic()

    if hasattr(spy.columns, 'get_level_values'):
        spy.columns = spy.columns.get_level_values(0)
    if hasattr(vix_df.columns, 'get_level_values'):
        vix_df.columns = vix_df.columns.get_level_values(0)

    spy_ret = spy["Close"].pct_change().dropna()
    vix_close = vix_df["Close"].reindex(spy_ret.index).ffill().dropna()
    common = spy_ret.index.intersection(vix_close.index)

    X = np.column_stack([
        spy_ret.loc[common].values,
        vix_close.loc[common].values / 100.0,
    ])

    model = hmm.GaussianHMM(n_components=4, covariance_type="diag", n_iter=200, random_state=42)
    model.fit(X)
    states = model.predict(X)
    probs  = model.predict_proba(X)

    # Map HMM states to regimes by mean SPY return in each state
    state_means = {}
    for s in range(4):
        mask = states == s
        if mask.sum() > 0:
            state_means[s] = float(spy_ret.loc[common].values[mask].mean())

    sorted_states = sorted(state_means, key=state_means.get)
    state_to_regime = {
        sorted_states[0]: "crash",
        sorted_states[1]: "bear",
        sorted_states[2]: "neutral",
        sorted_states[3]: "bull",
    }

    current_state = int(states[-1])
    current_regime = state_to_regime[current_state]
    current_conf   = float(probs[-1][current_state])

    vix_now = float(vix_close.iloc[-1])
    spy_ret_5d = float(spy_ret.iloc[-5:].sum())

    return RegimeResult(
        regime=current_regime,
        vix=vix_now,
        spy_return_5d=spy_ret_5d,
        confidence=current_conf,
        kelly_mult=REGIME_KELLY_MULTIPLIERS[current_regime],
        description=_regime_desc(current_regime, vix_now, spy_ret_5d),
    )


def _vix_heuristic() -> RegimeResult:
    """Simple VIX-threshold fallback when hmmlearn is unavailable."""
    try:
        import yfinance as yf
        vix_df = yf.download("^VIX", period="5d", interval="1d", progress=False, auto_adjust=True)
        spy_df = yf.download("SPY",  period="10d", interval="1d", progress=False, auto_adjust=True)

        if hasattr(vix_df.columns, 'get_level_values'):
            vix_df.columns = vix_df.columns.get_level_values(0)
        if hasattr(spy_df.columns, 'get_level_values'):
            spy_df.columns = spy_df.columns.get_level_values(0)

        vix = float(vix_df["Close"].iloc[-1]) if not vix_df.empty else 20.0
        spy_ret_5d = float(spy_df["Close"].pct_change().iloc[-5:].sum()) if not spy_df.empty else 0.0
    except Exception:
        vix, spy_ret_5d = 20.0, 0.0

    if vix >= 40 or spy_ret_5d <= -0.08:
        regime = "crash"
    elif vix >= 28 or spy_ret_5d <= -0.04:
        regime = "bear"
    elif vix >= 20 or spy_ret_5d <= -0.01:
        regime = "neutral"
    else:
        regime = "bull"

    return RegimeResult(
        regime=regime,
        vix=vix,
        spy_return_5d=spy_ret_5d,
        confidence=0.75,
        kelly_mult=REGIME_KELLY_MULTIPLIERS[regime],
        description=_regime_desc(regime, vix, spy_ret_5d),
    )


def _regime_desc(regime: str, vix: float, spy_5d: float) -> str:
    descs = {
        "bull":    f"BULL market — VIX={vix:.1f} (low fear), SPY 5d={spy_5d:+.1%}. Full position sizing.",
        "neutral": f"NEUTRAL market — VIX={vix:.1f}, SPY 5d={spy_5d:+.1%}. Reduced sizing (0.7x Kelly).",
        "bear":    f"BEAR market — VIX={vix:.1f} (elevated fear), SPY 5d={spy_5d:+.1%}. Defensive mode (0.4x Kelly).",
        "crash":   f"CRASH/PANIC — VIX={vix:.1f} (extreme fear), SPY 5d={spy_5d:+.1%}. Capital preservation mode (0.1x Kelly).",
    }
    return descs.get(regime, "")


def get_regime_as_context() -> str:
    """Return regime as a formatted string for agent prompts."""
    try:
        r = get_market_regime()
        emoji = {"bull": "🟢", "neutral": "🟡", "bear": "🔴", "crash": "💀"}.get(r.regime, "⚪")
        return (
            f"{emoji} MARKET REGIME: {r.regime.upper()} (confidence={r.confidence:.0%})\n"
            f"  {r.description}"
        )
    except Exception:
        return ""
