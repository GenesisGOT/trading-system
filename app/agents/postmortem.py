"""Post-mortem self-improvement loop.

After a position closes (stop hit, take-profit hit, or manual SELL), this
module runs a post-mortem LLM analysis that:
  1. Compares what the agents predicted vs what actually happened
  2. Identifies what signal would have caught the move earlier
  3. Stores lessons in Mem0 for future cycles

Called by the stop loss monitor after every exit.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


def run_postmortem(
    ticker: str,
    asset_type: str,
    side: str,
    entry_price: float,
    exit_price: float,
    entry_date: str,
    exit_reason: str,           # TRAILING_STOP | TAKE_PROFIT | SELL_SIGNAL
    peak_price: float,
    original_thesis: Optional[str] = None,
    analyst_reports: Optional[dict] = None,
) -> None:
    """Run async post-mortem analysis and store lessons in Mem0."""
    try:
        pnl_pct = (exit_price - entry_price) / entry_price
        outcome = "WIN" if pnl_pct > 0 else "LOSS"
        missed_gain_pct = (peak_price - exit_price) / entry_price if peak_price > exit_price else 0

        reports_text = ""
        if analyst_reports:
            reports_text = "\n".join(
                f"- {k.title()}: {str(v)[:150]}" for k, v in analyst_reports.items() if v
            )

        prompt = f"""You are a trading post-mortem analyst. Analyze this completed trade and extract lessons.

TRADE SUMMARY:
  Ticker: {ticker} ({asset_type})
  Side: {side.upper()}
  Entry: ${entry_price:.2f} on {entry_date}
  Exit: ${exit_price:.2f} ({exit_reason})
  P&L: {pnl_pct:+.1%} ({outcome})
  Peak price reached: ${peak_price:.2f} (missed {missed_gain_pct:+.1%} by exiting early)

ORIGINAL THESIS:
{original_thesis or "Not available"}

ANALYST REPORTS AT ENTRY:
{reports_text or "Not available"}

Answer these questions concisely:
1. Was the entry thesis correct? What did we miss?
2. Was the exit timing optimal? Should stops have been wider/tighter?
3. What one signal would have improved this trade most?
4. What should future agents know about {ticker} based on this trade?

Keep each answer to 1-2 sentences. Focus on actionable learnings."""

        from app.agents.graph import _get_llm
        llm = _get_llm(quick=True)
        analysis = llm.invoke(prompt).content

        # Store lesson in Mem0
        lesson = (
            f"Post-mortem {ticker} {outcome} ({pnl_pct:+.1%}) — {exit_reason}: {analysis[:500]}"
        )
        try:
            from app.agents.tools.memory import store_decision
            store_decision(
                ticker=ticker,
                action=side.upper(),
                confidence=abs(pnl_pct),
                thesis=lesson,
                outcome=outcome,
            )
        except Exception as exc:
            log.debug("Mem0 store failed: %s", exc)

        # Log to DB
        try:
            from app.database import log_system_event
            log_system_event(
                "postmortem",
                f"{ticker} {outcome} {pnl_pct:+.1%} | {analysis[:300]}",
            )
        except Exception:
            pass

        # Notify
        emoji = "✅" if outcome == "WIN" else "📉"
        from app.notifications import notify
        notify(
            f"{emoji} *Post-Mortem: {ticker}* ({outcome} {pnl_pct:+.1%})\n"
            f"Exit reason: {exit_reason}\n"
            f"_{analysis[:400]}_"
        )

        log.info("[%s] Post-mortem complete: %s %+.1f%%", ticker, outcome, pnl_pct * 100)

    except Exception as exc:
        log.error("[%s] Post-mortem failed: %s", ticker, exc)
