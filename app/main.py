"""FastAPI application — entry point for the cloud-hosted trading system."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from pathlib import Path

from app.config import settings
from app.database import (
    get_recent_decisions,
    get_recent_executions,
    get_daily_trade_count,
    get_win_rate_30d,
    init_db,
    log_system_event,
)
from app.notifications import notify

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting trading system — dry_run=%s symbols=%s", settings.dry_run, settings.allowed_symbols)
    init_db()
    log_system_event("startup", f"symbols={settings.allowed_symbols} dry_run={settings.dry_run}")

    # Restore trailing stops that survived a redeploy
    from app.agents.stop_loss_monitor import restore_positions_from_db
    restore_positions_from_db()

    from app.scheduler import start_scheduler
    start_scheduler()

    yield

    from app.scheduler import stop_scheduler
    stop_scheduler()
    log.info("Trading system shut down")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Multi-Agent Trading System",
    version="2.0.0",
    description="TradingAgents × Vibe-Trading × Robinhood MCP",
    lifespan=lifespan,
)

_templates_path = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_path))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response models ─────────────────────────────────────────────────

class HaltRequest(BaseModel):
    reason: str = "manual halt via API"


class StatusResponse(BaseModel):
    halted: bool
    dry_run: bool
    allowed_symbols: List[str]
    loop_interval_minutes: int
    mandate_max_order_usd: float
    mandate_daily_cap_usd: float
    mandate_max_trades_per_day: int
    daily_trades_today: int
    broker_status: Dict[str, Any]
    timestamp: str


class TriggerResponse(BaseModel):
    message: str
    run_id: str


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/status", response_model=StatusResponse)
async def get_status():
    from app.scheduler import is_halted
    from app.broker.connector import broker

    return StatusResponse(
        halted=is_halted(),
        dry_run=settings.dry_run,
        allowed_symbols=settings.allowed_symbols,
        loop_interval_minutes=settings.loop_interval_minutes,
        mandate_max_order_usd=settings.mandate_max_order_usd,
        mandate_daily_cap_usd=settings.mandate_daily_cap_usd,
        mandate_max_trades_per_day=settings.mandate_max_trades_per_day,
        daily_trades_today=get_daily_trade_count(),
        broker_status=broker.status(),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/halt", status_code=status.HTTP_200_OK)
async def halt_trading(body: HaltRequest):
    """Kill switch — immediately stops all future order submissions."""
    from app.scheduler import halt
    halt(body.reason)
    return {"halted": True, "reason": body.reason, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/resume", status_code=status.HTTP_200_OK)
async def resume_trading():
    """Re-enable trading after a halt."""
    from app.scheduler import resume, is_halted
    if not is_halted():
        return {"halted": False, "message": "System was not halted"}
    resume()
    return {"halted": False, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/trigger", response_model=TriggerResponse)
async def trigger_run():
    """Trigger an immediate agent analysis outside the scheduled interval."""
    from app.scheduler import is_halted, trigger_now
    if is_halted():
        raise HTTPException(status_code=409, detail="System is halted — call /resume first")

    import uuid
    run_id = str(uuid.uuid4())[:8]
    # Fire and forget — caller can poll /decisions for results
    asyncio.create_task(trigger_now())
    return TriggerResponse(message="Analysis triggered", run_id=run_id)


@app.get("/decisions")
async def list_decisions(limit: int = 50):
    """Return the most recent agent decisions from the audit ledger."""
    return get_recent_decisions(limit=min(limit, 200))


@app.get("/executions")
async def list_executions(limit: int = 50):
    """Return the most recent trade execution records."""
    return get_recent_executions(limit=min(limit, 200))


@app.get("/mandate")
async def get_mandate():
    """Return current mandate configuration."""
    return {
        "allowed_symbols": settings.allowed_symbols,
        "max_order_usd": settings.mandate_max_order_usd,
        "daily_cap_usd": settings.mandate_daily_cap_usd,
        "max_exposure_usd": settings.mandate_max_exposure_usd,
        "max_trades_per_day": settings.mandate_max_trades_per_day,
        "daily_trades_used": get_daily_trade_count(),
    }


@app.post("/notify/test")
async def test_notification(message: str = "Test notification from trading system"):
    """Send a test notification to verify webhook configuration."""
    notify(f"[TEST] {message}")
    return {"sent": True}


@app.get("/debug")
async def debug():
    """Debug endpoint — shows scanner output and config."""
    from app.scanner import scan_market
    from app.scheduler import _get_session
    import asyncio
    symbols = await asyncio.to_thread(scan_market)
    return {
        "session": _get_session(),
        "scanner_crypto_symbols": settings.scanner_crypto_symbols,
        "polygon_api_key_set": bool(settings.polygon_api_key),
        "tavily_api_key_set": bool(settings.tavily_api_key),
        "mem0_api_key_set": bool(settings.mem0_api_key),
        "scan_result": symbols,
    }


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Live trading dashboard."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/positions")
async def get_positions():
    """Return open positions with trailing stop details and live P&L."""
    from app.agents.stop_loss_monitor import get_open_stops
    from app.broker.robinhood_mcp import robinhood
    positions = []
    for pos in get_open_stops():
        current_price = None
        pnl_pct = None
        try:
            ctx = robinhood.get_market_context(pos.ticker, pos.asset_type)
            current_price = ctx.get("price")
            if current_price and pos.entry_price:
                pnl_pct = (current_price - pos.entry_price) / pos.entry_price
        except Exception:
            pass
        positions.append({
            "ticker": pos.ticker,
            "asset_type": pos.asset_type,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "high_water": pos.high_water,
            "trail_pct": pos.trail_pct,
            "quantity": pos.quantity,
            "notional": pos.notional,
            "order_id": pos.order_id,
            "opened_at": pos.opened_at,
            "current_price": current_price,
            "pnl_pct": pnl_pct,
        })
    return positions


@app.get("/metrics")
async def get_metrics():
    """Return dashboard metrics: win rate, Fear & Greed, today's P&L."""
    from app.agents.tools.market_sentiment import get_fear_greed
    metrics: Dict[str, Any] = {
        "win_rate_30d": get_win_rate_30d(),
        "daily_trades": get_daily_trade_count(),
        "pnl_today": None,
        "fear_greed_score": None,
        "fear_greed_label": None,
    }
    try:
        import httpx
        resp = httpx.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            timeout=8, headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code == 200:
            fg = resp.json()["fear_and_greed"]
            metrics["fear_greed_score"] = fg["score"]
            metrics["fear_greed_label"] = fg["rating"]
    except Exception:
        pass
    return metrics


# ── TradingView Webhook ───────────────────────────────────────────────────────

class TradingViewAlert(BaseModel):
    ticker: str
    action: str          # BUY | SELL
    price: Optional[float] = None
    confidence: float = 0.75
    asset_type: str = "stock"
    secret: Optional[str] = None


@app.post("/webhook/tradingview")
async def tradingview_webhook(alert: TradingViewAlert):
    """Receive TradingView Pine Script alerts and inject them as high-confidence signals.

    Pine Script usage:
      alertcondition(crossover(ema9, ema21), title="BUY Signal")
      alert('{"ticker":"{{ticker}}","action":"BUY","price":{{close}},"secret":"YOUR_SECRET"}', alert.freq_once_per_bar)
    """
    webhook_secret = getattr(settings, "tradingview_webhook_secret", None)
    if webhook_secret and alert.secret != webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    from app.scheduler import is_halted, _process_symbol
    if is_halted():
        return {"status": "halted", "message": "System is halted"}

    import uuid
    run_id = f"tv-{str(uuid.uuid4())[:6]}"
    log.info("[TradingView] %s %s @ $%s conf=%.2f", alert.action, alert.ticker, alert.price, alert.confidence)

    notify(
        f"📡 *TradingView Alert*\n"
        f"{alert.action} {alert.ticker} @ ${alert.price or 'market'}\n"
        f"Confidence: {alert.confidence:.0%} — queuing analysis"
    )

    asyncio.create_task(_process_symbol(run_id, alert.ticker.upper(), alert.asset_type))
    return {"status": "queued", "ticker": alert.ticker, "action": alert.action, "run_id": run_id}


# ── Backtest ──────────────────────────────────────────────────────────────────

@app.get("/backtest/{ticker}")
async def backtest(ticker: str, period: str = "1y", asset_type: str = "stock"):
    """Run a backtest on historical data for a ticker and return performance stats."""
    from app.backtesting import run_backtest
    result = await asyncio.to_thread(run_backtest, ticker.upper(), period, asset_type)
    if not result:
        raise HTTPException(status_code=500, detail="Backtest failed — check logs")
    return {
        "ticker": result.ticker,
        "period": result.period,
        "total_return": result.total_return,
        "sharpe_ratio": result.sharpe_ratio,
        "max_drawdown": result.max_drawdown,
        "win_rate": result.win_rate,
        "total_trades": result.total_trades,
        "avg_trade_return": result.avg_trade_return,
        "best_trade": result.best_trade,
        "worst_trade": result.worst_trade,
        "summary": result.summary,
    }


@app.get("/portfolio")
async def get_portfolio():
    """Return full portfolio summary from the active broker (Alpaca or Robinhood)."""
    if settings.broker == "alpaca":
        from app.broker.alpaca_connector import get_portfolio_summary
        return await asyncio.to_thread(get_portfolio_summary)
    from app.broker.robinhood_mcp import robinhood
    try:
        account   = robinhood.get_account()
        positions = robinhood.get_positions()
        return {"account": account, "positions": positions, "broker": "robinhood"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/regime")
async def market_regime():
    """Return current market regime (bull/neutral/bear/crash) and Kelly multiplier."""
    from app.agents.tools.regime import get_market_regime
    r = await asyncio.to_thread(get_market_regime)
    return {
        "regime": r.regime,
        "vix": r.vix,
        "spy_return_5d": r.spy_return_5d,
        "confidence": r.confidence,
        "kelly_multiplier": r.kelly_mult,
        "description": r.description,
    }
