"""Background scheduler — full market scan → multi-agent analysis → validate → execute.

Each cycle:
  1. Polygon.io scanner finds top movers across the entire US market
  2. TradingAgents runs 5 analysts + Bull/Bear debate + Fund Manager for each ticker
  3. Devil's Advocate validator stress-tests every BUY/SELL before execution
  4. Vibe-Trading + Robinhood MCP executes approved orders
  5. Rich Telegram/Discord notification with full thesis + reasoning
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
from app.notifications import (
    notify_error,
    notify_halt,
    notify_hold,
    notify_resume,
    notify_scan_start,
    notify_trade,
)
from app.broker.connector import broker

log = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None
_halted: bool = False


# ── Order sizing ─────────────────────────────────────────────────────────────

def _size_order(action: str, current_price: Optional[float]) -> tuple[float, Optional[float]]:
    notional = settings.mandate_max_order_usd
    if notional >= 999_999:
        # Unlimited mode — use account balance (Robinhood enforces the real cap)
        notional = None
    return 0.0, notional


# ── Per-symbol pipeline ───────────────────────────────────────────────────────

async def _process_symbol(run_id: str, ticker: str) -> None:
    if _halted or broker.is_halted():
        log.info("[%s] Skipping — halted", ticker)
        return

    log.info("[%s] Starting analysis run_id=%s", ticker, run_id)

    # ── 1. Run TradingAgents (blocking → thread) ──────────────────────────
    try:
        from app.agents.runner import run_analysis
        result = await asyncio.to_thread(
            run_analysis,
            ticker,
            settings.analysis_date_override or date.today().isoformat(),
        )
    except Exception as exc:
        log.error("[%s] Agent analysis failed: %s", ticker, exc, exc_info=True)
        log_system_event("error", f"{ticker}: agent failed — {exc}")
        notify_error(ticker, str(exc))
        return

    # ── 2. Log decision ───────────────────────────────────────────────────
    decision_id = log_decision(
        run_id=run_id,
        ticker=ticker,
        analysis_date=settings.analysis_date_override or date.today().isoformat(),
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

    # ── 3. Get market context for notification ────────────────────────────
    market_ctx = {}
    try:
        from app.scanner import get_ticker_context
        market_ctx = get_ticker_context(ticker)
    except Exception:
        pass

    # ── 4. HOLD — notify and skip ─────────────────────────────────────────
    if result.action == "HOLD":
        log.info("[%s] HOLD — no trade", ticker)
        notify_hold(ticker, result.rating, result.confidence, result.investment_thesis)
        return

    # ── 5. Validator stress-test ──────────────────────────────────────────
    try:
        from app.agents.validator import validate_decision
        validation = await asyncio.to_thread(
            validate_decision,
            ticker,
            result.action,
            result.rating,
            result.investment_thesis,
            result.analyst_reports,
        )
    except Exception as exc:
        log.error("[%s] Validator failed: %s", ticker, exc)
        from app.agents.validator import ValidationResult
        validation = ValidationResult(
            confidence=0.5, proceed=True,
            risk_summary=f"Validator error: {exc}", validation_notes="",
        )

    if not validation.proceed:
        log.warning("[%s] Validator blocked trade: confidence=%.2f", ticker, validation.confidence)
        log_execution(
            decision_id=decision_id, ticker=ticker, side=result.action.lower(),
            quantity=0, notional_usd=None, order_id=None,
            status="blocked", block_reason=f"Validator: confidence={validation.confidence:.2f} — {validation.risk_summary}",
            broker_response=None,
        )
        notify_trade(
            ticker=ticker, side=result.action.lower(), quantity=0, notional=None,
            status="blocked", order_id=None,
            block_reason=f"Low confidence ({validation.confidence:.0%}) — {validation.risk_summary}",
            rating=result.rating, confidence=result.confidence,
            investment_thesis=result.investment_thesis,
            price_target=result.price_target, time_horizon=result.time_horizon,
            risk_summary=validation.risk_summary, analyst_reports=result.analyst_reports,
            current_price=market_ctx.get("price"), change_pct=market_ctx.get("change_pct"),
            volume=market_ctx.get("volume"),
        )
        return

    # ── 6. Daily cap check ────────────────────────────────────────────────
    daily = get_daily_trade_count()
    if settings.mandate_max_trades_per_day < 999 and daily >= settings.mandate_max_trades_per_day:
        reason = f"Daily cap reached ({daily}/{settings.mandate_max_trades_per_day})"
        log.warning("[%s] %s", ticker, reason)
        log_execution(
            decision_id=decision_id, ticker=ticker, side=result.action.lower(),
            quantity=0, notional_usd=None, order_id=None,
            status="blocked", block_reason=reason, broker_response=None,
        )
        return

    # ── 7. Execute ────────────────────────────────────────────────────────
    side = result.action.lower()
    quantity, notional = _size_order(result.action, market_ctx.get("price"))

    order = broker.place_order(ticker, side, quantity, notional)
    log.info("[%s] Broker: status=%s order_id=%s", ticker, order.status, order.order_id)

    log_execution(
        decision_id=decision_id, ticker=ticker, side=side,
        quantity=quantity, notional_usd=notional, order_id=order.order_id,
        status=order.status, block_reason=order.block_reason,
        broker_response=order.broker_response,
    )

    notify_trade(
        ticker=ticker, side=side, quantity=quantity, notional=notional,
        status=order.status, order_id=order.order_id,
        block_reason=order.block_reason,
        rating=result.rating, confidence=validation.confidence,
        investment_thesis=result.investment_thesis,
        price_target=result.price_target, time_horizon=result.time_horizon,
        risk_summary=validation.risk_summary, analyst_reports=result.analyst_reports,
        current_price=market_ctx.get("price"), change_pct=market_ctx.get("change_pct"),
        volume=market_ctx.get("volume"),
    )


# ── Main loop ─────────────────────────────────────────────────────────────────

async def _run_loop() -> None:
    if _halted:
        log.info("Loop skipped — halted")
        return

    run_id = str(uuid.uuid4())[:8]
    log.info("=== Loop start run_id=%s ===", run_id)
    log_system_event("loop_start", f"run_id={run_id}")

    # Scan full market to discover tickers
    try:
        from app.scanner import scan_market
        tickers = await asyncio.to_thread(scan_market)
    except Exception as exc:
        log.error("Scanner failed: %s — falling back to config symbols", exc)
        tickers = settings.allowed_symbols

    log.info("Analyzing %d tickers: %s", len(tickers), tickers)
    notify_scan_start(tickers)

    for ticker in tickers:
        try:
            await _process_symbol(run_id, ticker)
        except Exception as exc:
            log.error("Unexpected error on %s: %s", ticker, exc, exc_info=True)
            notify_error(ticker, str(exc))

    log.info("=== Loop complete run_id=%s ===", run_id)
    log_system_event("loop_complete", f"run_id={run_id}")


# ── Halt / resume ─────────────────────────────────────────────────────────────

def halt(reason: str = "api kill-switch") -> None:
    global _halted
    _halted = True
    broker.halt(reason)
    log_system_event("halt", reason)
    notify_halt(reason)


def resume() -> None:
    global _halted
    _halted = False
    broker.resume()
    log_system_event("resume", "")
    notify_resume()


def is_halted() -> bool:
    return _halted or broker.is_halted()


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

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
        max_instances=1,
    )
    _scheduler.start()
    log.info("Scheduler started — interval=%dmin", settings.loop_interval_minutes)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


async def trigger_now() -> None:
    await _run_loop()
