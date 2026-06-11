"""Portfolio Brain — master agent that runs after each scan cycle.

Sees ALL decisions from the current cycle + ALL open positions + portfolio state
and produces portfolio-level guidance: sizing adjustments, concentration warnings,
cross-market correlations, and a unified market view.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

log = logging.getLogger(__name__)


def run_portfolio_brain(
    run_id: str,
    cycle_decisions: List[Dict],
    open_positions: List[Dict],
    portfolio: Dict,
    regime: str,
) -> Dict[str, Any]:
    """
    cycle_decisions: list of {ticker, asset_type, action, confidence, rating, thesis}
    open_positions:  list of current open stops/positions
    portfolio:       {equity, cash, buying_power, day_pnl, day_pnl_pct}
    regime:          e.g. "neutral VIX 22 0.7x Kelly"
    """
    try:
        from app.agents.graph import _get_llm
        from app.database import get_research_summary, log_system_event
        from app.scan_feed import emit

        # Build context
        decisions_text = _format_decisions(cycle_decisions)
        positions_text = _format_positions(open_positions)
        portfolio_text = _format_portfolio(portfolio)

        # Pull recent research summaries for held tickers
        held_tickers = [p.get("ticker", "") for p in open_positions]
        research_notes = []
        for t in held_tickers[:5]:
            summary = get_research_summary(t)
            if summary:
                research_notes.append(summary)

        prompt = f"""You are a Portfolio Manager AI overseeing a multi-asset trading portfolio.
You have just completed a scan cycle. Review everything and provide portfolio-level guidance.

=== PORTFOLIO STATE ===
{portfolio_text}
Market Regime: {regime}

=== THIS CYCLE'S DECISIONS ===
{decisions_text or "No new decisions this cycle."}

=== OPEN POSITIONS ===
{positions_text or "No open positions."}

=== RECENT RESEARCH ===
{chr(10).join(research_notes) if research_notes else "No prior research."}

Your job:
1. PORTFOLIO HEALTH — is the portfolio well-diversified or over-concentrated in one area?
2. RISK FLAGS — flag any positions that should be trimmed/cut given new info
3. BEST PICK THIS CYCLE — which decision (if any) has the highest conviction?
4. CROSS-MARKET VIEW — are there conflicting signals across asset classes?
5. ACTION ITEMS — 2-3 specific things to do or watch (e.g. "NYK series 2-2, consider trimming if Spurs win Game 5")

Prediction market context: User holds NYK (60 contracts, series 2-2 Spurs momentum),
Topuria UFC Jun 14, England/France WC pre-tournament, BTC<55K (near total loss), CPI June macro.

Be direct and specific. Max 250 words. End with a PORTFOLIO RATING: STRONG / BALANCED / WEAK / CRITICAL.
"""

        llm = _get_llm(quick=False)
        response = llm.invoke(prompt).content

        result = {
            "run_id": run_id,
            "analysis": response,
            "decisions_reviewed": len(cycle_decisions),
            "positions_reviewed": len(open_positions),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Emit to live feed
        emit("portfolio_brain", {
            "run_id": run_id,
            "analysis": response[:500],
            "decisions": len(cycle_decisions),
            "positions": len(open_positions),
        })

        # Save to system events
        log_system_event("portfolio_brain", response[:500])

        log.info("[portfolio_brain] Analysis complete for run_id=%s", run_id)
        return result

    except Exception as exc:
        log.error("[portfolio_brain] Failed: %s", exc, exc_info=True)
        return {"run_id": run_id, "error": str(exc)}


def _format_decisions(decisions: List[Dict]) -> str:
    if not decisions:
        return ""
    lines = []
    for d in decisions:
        lines.append(
            f"• {d.get('ticker')} ({d.get('asset_type')}) → {d.get('action')} "
            f"conf={d.get('confidence', 0):.0%} | {d.get('rating')} | {str(d.get('thesis', ''))[:100]}"
        )
    return "\n".join(lines)


def _format_positions(positions: List[Dict]) -> str:
    if not positions:
        return ""
    lines = []
    for p in positions:
        pnl = p.get("pnl_pct")
        pnl_str = f"{pnl*100:+.1f}%" if pnl is not None else "?"
        lines.append(
            f"• {p.get('ticker')} ({p.get('asset_type')}) "
            f"entry={p.get('entry_price')} qty={p.get('quantity')} P&L={pnl_str}"
        )
    return "\n".join(lines)


def _format_portfolio(portfolio: Dict) -> str:
    equity = portfolio.get("equity", 0)
    cash = portfolio.get("cash", 0)
    day_pnl = portfolio.get("day_pnl", 0)
    day_pnl_pct = portfolio.get("day_pnl_pct", 0)
    positions = portfolio.get("open_positions", 0)
    return (
        f"Equity: ${equity:,.2f} | Cash: ${cash:,.2f} | "
        f"Day P&L: ${day_pnl:+,.2f} ({day_pnl_pct*100:+.2f}%) | "
        f"Open Positions: {positions}"
    )
