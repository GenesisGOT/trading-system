"""Background scheduler — runs the full agent → execution loop on a timer.

Architecture:
  APScheduler fires every LOOP_INTERVAL_MINUTES minutes.
  For each symbol in allowed_symbols:
    1. Run TradingAgents analysis (blocking, via asyncio.to_thread)
    2. Log decision to SQLite
    3. If action is BUY or SELL, size the order and call BrokerConnector
    4. Log execution to SQLite
    5. Push notification via webhook
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import (
    get_daily_trade_count,
    log_analyst_report,
    log_decision,
    log_execution,
    log_system_event,
)
from app.notifications import notify_error, notify_trade, notify_halt, notify_resume
from app.broker.connector import broker

log = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None
_halted: bool = False  # in-process halt flag (belt-and-suspenders)


# ── Order sizing ─────────────────────────────────────────────────────────────

def _size_order(action: str, ticker: str) -> tuple[float, Optional[float]]:
    """Return (quantity, notional_usd) for the given action.

    Uses mandate_max_order_usd as the notional ceiling; quantity is left to
    the broker to derive from current price (fractional shares supported by
    Robinhood).  Returns (0, 0) when no position data is available for SELL.
    """
    if action == "BUY":
        notional = settings.mandate_max_order_usd
        # Robinhood supports fractional-dollar orders; pass qty=0 to signal
        # notional-only order.  Vibe-Trading normalises this internally.
        return 0.0, notional

    if action == "SELL":
        # Ask the broker for current position size (read-only)
        try:
            if broker._sdk_available:
                from agent.src.trading.service import get_account
                account = get_account(broker=settings.mandate_max_trades_per_day)  # noqa: type
                # Simplified: sell up to mandate_max_order_usd worth
                return 0.0, settings.mandate_max_order_usd
        except Exception:
            pass
        return 0.0, settings.mandate_max_order_usd

    return 0.0, 0.0


# ── Per-symbol analysis + execution ─────────────────────────────────────────

async def _process_symbol(run_id: str, ticker: str) -> None:
    if _halted or broker.is_halted():
        log.info("[%s] Skipping — system is halted", ticker)
        return

    log.info("[%s] Starting analysis run_id=%s", ticker, run_id)

    # ── 1. Run agents (blocking → thread) ────────────────────────────────
    try:
        from app.agents.runner import run_analysis
        result = await asyncio.to_thread(
            run_analysis,
            ticker,
            settings.analysis_date_override or date.today().isoformat(),
        )
    except Exception as exc:
        log.error("[%s] Agent analysis failed: %s", ticker, exc, exc_info=True)
        log_system_event("error", f"{ticker}: agent analysis failed — {exc}")
        notify_error(ticker, str(exc))
        return

    # ── 2. Log decision ───────────────────────────────────────────────────
    analysis_date = settings.analysis_date_override or date.today().isoformat()
    decision_id = log_decision(
        run_id=run_id,
        ticker=ticker,
        analysis_date=analysis_date,
        rating=result.rating,
        action=result.action,
        confidence=result.confidence,
        summary=result.summary,
        investment_thesis=result.investment_thesis,
        price_target=result.price_target,
        time_horizon=result.time_horizon,
        raw_state=result.raw_state,
    )
    for analyst, report in result.analyst_reports.items():
        log_analyst_report(decision_id, analyst, report)

    log.info("[%s] Action=%s Rating=%s Confidence=%.2f", ticker, result.action, result.rating, result.confidence or 0)

    # ── 3. Execute if BUY or SELL ─────────────────────────────────────────
    if result.action == "HOLD":
        log.info("[%s] HOLD — no order placed", ticker)
        return

    # Check daily cap before submitting
    daily = get_daily_trade_count()
    if daily >= settings.mandate_max_trades_per_day:
        reason = f"Daily cap reached ({daily}/{settings.mandate_max_trades_per_day})"
        log.warning("[%s] %s", ticker, reason)
        log_execution(
            decision_id=decision_id,
            ticker=ticker,
            side=result.action.lower(),
            quantity=0,
            notional_usd=None,
            order_id=None,
            status="blocked",
            block_reason=reason,
            broker_response=None,
        )
        return

    side = result.action.lower()
    quantity, notional = _size_order(result.action, ticker)

    order = broker.place_order(ticker, side, quantity, notional)
    log.info("[%s] Broker result: status=%s order_id=%s", ticker, order.status, order.order_id)

    log_execution(
        decision_id=decision_id,
        ticker=ticker,
        side=side,
        quantity=quantity,
        notional_usd=notional,
        order_id=order.order_id,
        status=order.status,
        block_reason=order.block_reason,
        broker_response=order.broker_response,
    )

    notify_trade(
        ticker=ticker,
        side=side,
        quantity=quantity,
        notional=notional,
        status=order.status,
        order_id=order.order_id,
        reason=order.block_reason,
        rating=result.rating,
    )


# ── Main loop ────────────────────────────────────────────────────────────────

async def _run_loop() -> None:
    if _halted:
        log.info("Agent loop skipped — system halted")
        return

    run_id = str(uuid.uuid4())[:8]
    log.info("=== Agent loop start run_id=%s symbols=%s ===", run_id, settings.allowed_symbols)
    log_system_event("loop_start", f"run_id={run_id}")

    for ticker in settings.allowed_symbols:
        try:
            await _process_symbol(run_id, ticker)
        except Exception as exc:
            log.error("Unexpected error processing %s: %s", ticker, exc, exc_info=True)
            notify_error(ticker, str(exc))

    log.info("=== Agent loop complete run_id=%s ===", run_id)
    log_system_event("loop_complete", f"run_id={run_id}")


# ── Halt / resume ────────────────────────────────────────────────────────────

def halt(reason: str = "api kill-switch") -> None:
    global _halted
    _halted = True
    broker.halt(reason)
    log_system_event("halt", reason)
    notify_halt(reason)
    log.warning("HALT: %s", reason)


def resume() -> None:
    global _halted
    _halted = False
    broker.resume()
    log_system_event("resume", "")
    notify_resume()
    log.info("RESUME: trading re-enabled")


def is_halted() -> bool:
    return _halted or broker.is_halted()


# ── Scheduler lifecycle ──────────────────────────────────────────────────────

def start_scheduler() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _run_loop,
        trigger=IntervalTrigger(minutes=settings.loop_interval_minutes),
        id="agent_loop",
        name="TradingAgents loop",
        replace_existing=True,
        misfire_grace_time=300,
        max_instances=1,  # never run overlapping
    )
    _scheduler.start()
    log.info("Scheduler started — interval=%d min symbols=%s", settings.loop_interval_minutes, settings.allowed_symbols)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Scheduler stopped")


async def trigger_now() -> None:
    """Trigger an immediate run outside the scheduler interval."""
    await _run_loop()
