"""Background scheduler — full market scan → LangGraph multi-agent analysis → execute.

Each cycle:
  1. Scanner finds top stocks + crypto + options + prediction markets
  2. LangGraph runs 5 parallel analysts → Bull/Bear debate (with re-debate loop) →
     Fund Manager → Validator for each symbol
  3. Approved orders are sized and sent to Robinhood via broker connector
  4. Telegram/Discord rich notification with full thesis + reasoning
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date
from typing import Optional, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import pytz
from datetime import datetime

from app.config import settings
from app.database import (
    get_daily_trade_count,
    get_incomplete_scan,
    log_analyst_report,
    log_decision,
    log_execution,
    log_system_event,
    save_research,
    save_scan_symbols,
    upsert_scan_state,
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


# ── Order sizing ──────────────────────────────────────────────────────────────

def _size_order(
    action: str,
    asset_type: str,
    current_price: Optional[float],
    confidence: float = 0.5,
    buying_power: float = 0.0,
    ticker: str = "",
) -> tuple[float, Optional[float]]:
    if settings.mandate_max_order_usd >= 999_999:
        return 0.0, None  # unlimited — broker enforces the real cap
    try:
        from app.sizing import kelly_size
        notional = kelly_size(
            confidence=confidence,
            buying_power=buying_power if buying_power > 0 else settings.mandate_max_order_usd,
            ticker=ticker,
            asset_type=asset_type,
        )
    except Exception as exc:
        log.warning("Kelly sizing failed (%s) — using flat cap", exc)
        notional = settings.mandate_max_order_usd
    return 0.0, notional


# ── Per-symbol pipeline ───────────────────────────────────────────────────────

async def _process_symbol(run_id: str, ticker: str, asset_type: str) -> None:
    if _halted or broker.is_halted():
        log.info("[%s] Skipping — halted", ticker)
        return

    log.info("[%s] Starting LangGraph analysis run_id=%s asset_type=%s", ticker, run_id, asset_type)
    upsert_scan_state(run_id, ticker, asset_type, "started")
    try:
        from app.scan_feed import emit_ticker_start
        emit_ticker_start(run_id, ticker, asset_type)
    except Exception:
        pass

    analysis_date = settings.analysis_date_override or date.today().isoformat()

    # ── 1. Run LangGraph pipeline ─────────────────────────────────────────
    try:
        from app.agents.graph import run_graph
        result = await asyncio.to_thread(run_graph, ticker, asset_type, analysis_date)
    except Exception as exc:
        log.error("[%s] Graph analysis failed: %s", ticker, exc, exc_info=True)
        log_system_event("error", f"{ticker}: graph failed — {exc}")
        notify_error(ticker, str(exc))
        return

    # ── 2. Log decision + emit to live feed ──────────────────────────────
    try:
        from app.scan_feed import emit_decision
        emit_decision(run_id, ticker, result.action, result.confidence,
                      result.rating, result.investment_thesis)
    except Exception:
        pass

    decision_id = log_decision(
        run_id=run_id,
        ticker=ticker,
        analysis_date=analysis_date,
        rating=result.rating,
        action=result.action,
        confidence=result.confidence,
        summary=result.investment_thesis[:200],
        investment_thesis=result.investment_thesis,
        price_target=result.price_target,
        time_horizon=result.time_horizon,
        raw_state=result.raw_state,
    )
    for analyst, report in result.analyst_reports.items():
        if report:
            log_analyst_report(decision_id, analyst, report)
            save_research(
                ticker=ticker,
                analyst=analyst,
                full_report=report,
                action=result.action,
                confidence=result.confidence,
                asset_type=asset_type,
                signals={"rating": result.rating, "price_target": result.price_target},
                tags=[result.action, asset_type, analyst],
            )

    # ── 3. Store decision in Mem0 for future cycles ───────────────────────
    try:
        from app.agents.tools.memory import store_decision
        store_decision(ticker, result.action, result.confidence, result.investment_thesis)
    except Exception:
        pass

    # ── 4. Get market context for notification ────────────────────────────
    market_ctx = {}
    if asset_type == "stock":
        try:
            from app.scanner import get_ticker_context
            market_ctx = get_ticker_context(ticker)
        except Exception:
            pass

    # ── 5. HOLD — notify and skip ─────────────────────────────────────────
    if result.action == "HOLD":
        log.info("[%s] HOLD — no trade", ticker)
        notify_hold(ticker, result.rating, result.confidence, result.investment_thesis)
        upsert_scan_state(run_id, ticker, asset_type, "completed")
        return

    # ── 6. Validator already ran inside the graph — check result ─────────
    if not result.validation_proceed:
        log.warning("[%s] Validator blocked: confidence=%.2f", ticker, result.validation_confidence)
        log_execution(
            decision_id=decision_id, ticker=ticker, side=result.action.lower(),
            quantity=0, notional_usd=None, order_id=None,
            status="blocked",
            block_reason=f"Validator: {result.validation_confidence:.0%} — {result.risk_summary}",
            broker_response=None,
        )
        notify_trade(
            ticker=ticker, side=result.action.lower(), quantity=0, notional=None,
            status="blocked", order_id=None,
            block_reason=f"Low confidence ({result.validation_confidence:.0%}) — {result.risk_summary}",
            rating=result.rating, confidence=result.confidence,
            investment_thesis=result.investment_thesis,
            price_target=result.price_target, time_horizon=result.time_horizon,
            risk_summary=result.risk_summary, analyst_reports=result.analyst_reports,
            current_price=market_ctx.get("price"), change_pct=market_ctx.get("change_pct"),
            volume=market_ctx.get("volume"), asset_type=asset_type,
        )
        return

    # ── 7. Correlation filter — skip BUY if already holding a correlated position ─
    if result.action == "BUY" and _is_correlated_with_existing(ticker, asset_type):
        log.info("[%s] Skipping BUY — highly correlated with existing open position", ticker)
        log_execution(
            decision_id=decision_id, ticker=ticker, side="buy",
            quantity=0, notional_usd=None, order_id=None,
            status="blocked", block_reason="Correlation filter: similar position already open",
            broker_response=None,
        )
        return

    # ── 8. Daily cap check ───────────────────────────────────────────────────
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

    # ── 8. Execute ────────────────────────────────────────────────────────
    side = result.action.lower()
    quantity, notional = _size_order(
        result.action, asset_type, market_ctx.get("price"),
        confidence=result.confidence, buying_power=market_ctx.get("buying_power", 0),
        ticker=ticker,
    )

    order = broker.place_order(ticker, side, quantity, notional, asset_type=asset_type)
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
        rating=result.rating, confidence=result.validation_confidence,
        investment_thesis=result.investment_thesis,
        price_target=result.price_target, time_horizon=result.time_horizon,
        risk_summary=result.risk_summary, analyst_reports=result.analyst_reports,
        current_price=market_ctx.get("price"), change_pct=market_ctx.get("change_pct"),
        volume=market_ctx.get("volume"), asset_type=asset_type,
    )

    upsert_scan_state(run_id, ticker, asset_type, "completed")

    # ── Register stop loss / take profit for executed BUY orders ──────────
    if order.status in ("submitted", "dry_run") and side == "buy":
        entry = market_ctx.get("price", 0)
        if entry:
            try:
                from app.agents.stop_loss_monitor import register_stop
                from app.agents.tools.indicators import suggested_stop_loss
                atr_stop = suggested_stop_loss(ticker, entry, asset_type)
                register_stop(
                    ticker=ticker,
                    asset_type=asset_type,
                    side=side,
                    entry_price=entry,
                    quantity=quantity,
                    notional=notional,
                    order_id=order.order_id,
                    atr_stop=result.stop_loss or atr_stop,
                    take_profit_pct=0.10,
                )
            except Exception as exc:
                log.warning("[%s] Stop registration failed: %s", ticker, exc)


# ── Session detection ─────────────────────────────────────────────────────────

def _get_session() -> str:
    """Return current market session: pre_market | regular | after_hours | closed | crypto_only."""
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    weekday = now.weekday()  # 0=Mon, 6=Sun

    if weekday >= 5:  # Weekend
        return "crypto_only"

    hour = now.hour + now.minute / 60
    if 7 <= hour < 9.5:
        return "pre_market"
    if 9.5 <= hour < 16:
        return "regular"
    if 16 <= hour < 20:
        return "after_hours"
    return "crypto_only"


def _should_analyze(ticker: str, asset_type: str, session: str) -> bool:
    """Gate which assets trade in which sessions."""
    if asset_type in ("crypto", "prediction"):
        return True  # 24/7
    if session == "crypto_only":
        return False  # stocks/options closed on weekends + overnight
    return True  # pre_market, after_hours, regular — all run


# ── Main loop ─────────────────────────────────────────────────────────────────

def _check_drawdown_circuit_breaker() -> bool:
    """Auto-halt if portfolio drawdown exceeds threshold today. Returns True if halted."""
    try:
        from app.broker.robinhood_mcp import robinhood
        account = robinhood.get_account()
        equity  = float(account.get("equity", 0) or 0)
        prev_eq = float(account.get("equity_previous_close", equity) or equity)
        if prev_eq <= 0 or equity <= 0:
            return False
        drawdown = (equity - prev_eq) / prev_eq
        threshold = -abs(settings.max_drawdown_halt_pct)
        if drawdown <= threshold:
            reason = f"Max drawdown circuit breaker: {drawdown:.1%} (threshold {threshold:.1%})"
            log.warning(reason)
            halt(reason)
            return True
    except Exception as exc:
        log.debug("Circuit breaker check failed: %s", exc)
    return False


def _is_correlated_with_existing(ticker: str, asset_type: str) -> bool:
    """Return True if ticker is highly correlated with an already-open position."""
    if asset_type != "stock":
        return False
    try:
        from app.agents.stop_loss_monitor import get_open_stops
        open_tickers = [p.ticker for p in get_open_stops() if p.asset_type == "stock" and p.ticker != ticker]
        if not open_tickers:
            return False

        import yfinance as yf
        import pandas as pd
        symbols = [ticker] + open_tickers
        df = yf.download(symbols, period="60d", interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            return False

        close = df["Close"] if "Close" in df.columns else df.xs("Close", axis=1, level=0)
        if isinstance(close, pd.Series):
            return False

        corr = close.pct_change().corr()
        if ticker not in corr.columns:
            return False

        for existing in open_tickers:
            if existing in corr.columns:
                c = float(corr.loc[ticker, existing])
                if c >= settings.correlation_max_overlap:
                    log.info("[%s] Correlated %.2f with open position %s — skipping", ticker, c, existing)
                    return True
    except Exception as exc:
        log.debug("Correlation check failed: %s", exc)
    return False


async def _run_loop(resume: bool = False) -> None:
    if _halted:
        log.info("Loop skipped — halted")
        return

    # Circuit breaker — auto-halt on excessive drawdown
    if _check_drawdown_circuit_breaker():
        log.warning("Circuit breaker triggered — loop aborted")
        return

    # Check for an incomplete scan to resume
    if resume:
        incomplete = get_incomplete_scan()
        if incomplete:
            run_id, pending = incomplete
            log.info("=== Resuming interrupted scan run_id=%s — %d tickers remaining ===", run_id, len(pending))
            log_system_event("loop_resume", f"run_id={run_id} pending={len(pending)}")
            tradeable = pending
        else:
            resume = False  # nothing to resume, fall through to fresh scan

    if not resume:
        run_id = str(uuid.uuid4())[:8]
        log.info("=== Loop start run_id=%s ===", run_id)
        log_system_event("loop_start", f"run_id={run_id}")

        try:
            from app.scanner import scan_market
            symbols = await asyncio.to_thread(scan_market)
        except Exception as exc:
            log.error("Scanner failed: %s — falling back to config symbols", exc)
            crypto = [s.strip().upper() for s in settings.scanner_crypto_symbols.split(",") if s.strip()]
            symbols = [(s, "stock") for s in settings.allowed_symbols] + [(s, "crypto") for s in crypto]

        session = _get_session()
        log.info("Market session: %s", session)
        tradeable = [(t, a) for t, a in symbols if _should_analyze(t, a, session)]
        skipped = len(symbols) - len(tradeable)
        if skipped:
            log.info("Skipping %d symbols (session=%s)", skipped, session)

        # Persist all symbols upfront so restarts can resume
        save_scan_symbols(run_id, tradeable)

    session = _get_session()
    notify_scan_start([f"{t}({a})" for t, a in tradeable], session=session)

    try:
        from app.scan_feed import emit_scan_start, emit_category_start, emit_category_complete
        emit_scan_start(run_id, [f"{t}({a})" for t, a in tradeable], session)
    except Exception:
        pass

    async def _safe_process(ticker: str, asset_type: str) -> None:
        try:
            await _process_symbol(run_id, ticker, asset_type)
        except Exception as exc:
            log.error("Unexpected error on %s: %s", ticker, exc, exc_info=True)
            notify_error(ticker, str(exc))
            try:
                from app.scan_feed import emit_error
                emit_error(ticker, str(exc))
            except Exception:
                pass

    # ── Scan category by category, emitting picks between each wave ───────
    CATEGORY_ORDER = ["prediction", "crypto", "stock", "option"]

    total_analyzed = 0
    for category in CATEGORY_ORDER:
        batch = [(t, a) for t, a in tradeable if a == category]
        if not batch:
            continue

        log.info("=== Category wave: %s (%d tickers) ===", category, len(batch))
        try:
            from app.scan_feed import emit_category_start, emit_category_complete
            emit_category_start(run_id, category, [t for t, _ in batch])
        except Exception:
            pass

        await asyncio.gather(*[_safe_process(t, a) for t, a in batch])
        total_analyzed += len(batch)

        try:
            from app.scan_feed import emit_category_complete
            emit_category_complete(run_id, category, len(batch))
        except Exception:
            pass

        log.info("=== Category wave complete: %s ===", category)

    log.info("=== Loop complete run_id=%s (%d analyzed) ===", run_id, total_analyzed)
    log_system_event("loop_complete", f"run_id={run_id}")
    try:
        from app.scan_feed import emit_scan_complete
        emit_scan_complete(run_id, total_analyzed)
    except Exception:
        pass

    # ── Portfolio Brain — runs after all categories complete ──────────────
    if total_analyzed > 0:
        try:
            from app.agents.portfolio_brain import run_portfolio_brain
            from app.database import get_recent_decisions, load_positions

            # Pull this cycle's decisions from DB
            recent = get_recent_decisions(limit=50)
            cycle_decisions = [
                {
                    "ticker": d["ticker"],
                    "asset_type": d.get("raw_state", "{}"),
                    "action": d["action"],
                    "confidence": d["confidence"] or 0.0,
                    "rating": d["rating"],
                    "thesis": d["investment_thesis"] or "",
                }
                for d in recent
                if d["run_id"] == run_id
            ]

            # Open positions
            open_positions = load_positions()

            # Portfolio state from broker
            try:
                portfolio_state = broker.get_portfolio()
            except Exception:
                portfolio_state = {}

            session_label = _get_session()
            await asyncio.to_thread(
                run_portfolio_brain,
                run_id,
                cycle_decisions,
                open_positions,
                portfolio_state,
                session_label,
            )
        except Exception as exc:
            log.warning("Portfolio Brain failed: %s", exc)


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

async def _run_crypto_loop() -> None:
    """Faster loop for crypto-only during off-hours."""
    if _halted:
        return
    run_id = str(uuid.uuid4())[:8]
    try:
        from app.scanner import scan_market
        symbols = await asyncio.to_thread(scan_market)
    except Exception:
        return
    crypto_symbols = [(t, a) for t, a in symbols if a == "crypto"]
    if not crypto_symbols:
        return
    log.info("[crypto-loop] run_id=%s symbols=%s", run_id, crypto_symbols)
    await asyncio.gather(*[
        _process_symbol(run_id, t, a) for t, a in crypto_symbols
    ], return_exceptions=True)


async def _check_stops_job() -> None:
    """APScheduler wrapper for stop loss / take profit monitor."""
    try:
        from app.agents.stop_loss_monitor import check_stops
        await check_stops()
    except Exception as exc:
        log.error("Stop monitor error: %s", exc)


def start_scheduler() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler()

    # Main loop — all assets, session-aware
    _scheduler.add_job(
        _run_loop,
        trigger=IntervalTrigger(minutes=settings.loop_interval_minutes),
        id="agent_loop",
        name="Full market loop",
        replace_existing=True,
        misfire_grace_time=300,
        max_instances=1,
    )

    # Faster crypto loop — runs every 20 min 24/7
    _scheduler.add_job(
        _run_crypto_loop,
        trigger=IntervalTrigger(minutes=settings.crypto_loop_interval_minutes),
        id="crypto_loop",
        name="Crypto 24/7 loop",
        replace_existing=True,
        misfire_grace_time=120,
        max_instances=1,
    )

    # Stop loss / take profit monitor — every 5 minutes
    _scheduler.add_job(
        _check_stops_job,
        trigger=IntervalTrigger(minutes=5),
        id="stop_monitor",
        name="Stop loss / take profit monitor",
        replace_existing=True,
        misfire_grace_time=60,
        max_instances=1,
    )

    _scheduler.start()
    log.info("Scheduler started — main=%dmin crypto=%dmin stops=5min",
             settings.loop_interval_minutes, settings.crypto_loop_interval_minutes)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


async def trigger_now() -> None:
    await _run_loop()


async def resume_incomplete_scan() -> None:
    """Called on startup — resumes interrupted scan or starts fresh if overdue."""
    await asyncio.sleep(5)  # let the server fully start first

    from app.database import get_incomplete_scan, get_last_scan_completed_seconds_ago

    # 1. Resume mid-run scan if one exists
    incomplete = get_incomplete_scan()
    if incomplete:
        log.info("[startup] Resuming interrupted scan from previous deploy")
        await _run_loop(resume=True)
        return

    # 2. Check if we're overdue for a scan
    seconds_ago = get_last_scan_completed_seconds_ago()
    interval_seconds = settings.loop_interval_minutes * 60

    if seconds_ago is None:
        log.info("[startup] No previous scan found — starting fresh scan")
        await _run_loop()
    elif seconds_ago >= interval_seconds:
        log.info("[startup] Last scan was %.0fs ago (interval=%ds) — running immediately", seconds_ago, interval_seconds)
        await _run_loop()
    else:
        remaining = interval_seconds - seconds_ago
        log.info("[startup] Last scan was %.0fs ago — next scan in %.0fs (scheduler will handle)", seconds_ago, remaining)
