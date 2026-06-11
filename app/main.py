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
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from pathlib import Path
import json

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

    # Seed prediction contracts from env var if DB is empty
    from app.database import get_active_prediction_contracts, upsert_prediction_contract
    if not get_active_prediction_contracts() and settings.prediction_symbols:
        for sym in settings.prediction_symbols.split(","):
            sym = sym.strip().upper()
            if sym:
                upsert_prediction_contract(sym)
                log.info("Seeded prediction contract: %s", sym)

    from app.scheduler import start_scheduler, resume_incomplete_scan
    start_scheduler()

    # Resume any scan that was interrupted by the previous deploy
    asyncio.create_task(resume_incomplete_scan())

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


@app.get("/feed")
async def scan_feed(request: Request):
    """Server-Sent Events stream — live scan progress for the dashboard."""
    from app.scan_feed import subscribe, unsubscribe

    q = await subscribe()

    async def event_stream():
        try:
            # Send a heartbeat immediately so the connection is confirmed
            yield "data: {\"type\":\"connected\"}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield msg
                except asyncio.TimeoutError:
                    yield "data: {\"type\":\"heartbeat\"}\n\n"
        finally:
            unsubscribe(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/predictions")
async def get_predictions():
    """Return all active prediction market contracts."""
    from app.database import get_active_prediction_contracts
    return get_active_prediction_contracts()


class PredictionContract(BaseModel):
    symbol: str
    name: str = ""
    category: str = ""
    entry_price: float = 0.0
    quantity: int = 0


@app.post("/predictions")
async def add_prediction(contract: PredictionContract):
    """Add or update a prediction market contract to track."""
    from app.database import upsert_prediction_contract
    upsert_prediction_contract(
        symbol=contract.symbol,
        name=contract.name,
        category=contract.category,
        entry_price=contract.entry_price,
        quantity=contract.quantity,
    )
    return {"status": "ok", "symbol": contract.symbol.upper()}


@app.delete("/predictions/{symbol}")
async def remove_prediction(symbol: str):
    """Deactivate a prediction contract (won't be scanned anymore)."""
    from app.database import deactivate_prediction_contract
    deactivate_prediction_contract(symbol)
    return {"status": "ok", "symbol": symbol.upper()}


@app.get("/research")
async def get_research(ticker: str = None, analyst: str = None, limit: int = 50):
    """Query the analyst research library."""
    from app.database import get_research_for_ticker, get_research_by_analyst, get_db
    if ticker:
        return get_research_for_ticker(ticker.upper(), limit=min(limit, 200))
    if analyst:
        return get_research_by_analyst(analyst, limit=min(limit, 200))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM research_library ORDER BY created_at DESC LIMIT ?",
            (min(limit, 200),)
        ).fetchall()
        return [dict(r) for r in rows]


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
    """Return live positions from broker merged with trailing stop data."""
    from app.agents.stop_loss_monitor import get_open_stops

    # Pull live positions directly from broker
    broker_positions = []
    try:
        if settings.broker == "alpaca":
            from app.broker.alpaca_connector import get_positions as alpaca_positions, get_quote as alpaca_quote
            raw = alpaca_positions()
            for p in raw:
                sym = (p.get("symbol") or "").upper()
                qty = float(p.get("qty") or 0)
                entry = float(p.get("avg_entry_price") or 0)
                current = float(p.get("current_price") or 0)
                market_val = float(p.get("market_value") or 0)
                unrealized_pl = float(p.get("unrealized_pl") or 0)
                unrealized_plpc = float(p.get("unrealized_plpc") or 0)
                asset_class = p.get("asset_class", "us_equity")
                asset_type = "crypto" if asset_class == "crypto" else "stock"
                broker_positions.append({
                    "ticker": sym,
                    "asset_type": asset_type,
                    "side": "buy",
                    "entry_price": entry,
                    "current_price": current,
                    "quantity": qty,
                    "market_value": market_val,
                    "unrealized_pl": unrealized_pl,
                    "pnl_pct": unrealized_plpc,
                    "source": "broker",
                })
    except Exception as exc:
        log.warning("Failed to fetch broker positions: %s", exc)

    # Overlay our stop/take-profit data
    stops_by_ticker = {p.ticker: p for p in get_open_stops()}
    for pos in broker_positions:
        stop = stops_by_ticker.get(pos["ticker"])
        if stop:
            pos["stop_loss"] = stop.stop_loss
            pos["take_profit"] = stop.take_profit
            pos["high_water"] = stop.high_water
            pos["trail_pct"] = stop.trail_pct
            pos["order_id"] = stop.order_id
            pos["opened_at"] = stop.opened_at

    # Include any stops for positions broker doesn't know about yet
    broker_tickers = {p["ticker"] for p in broker_positions}
    for ticker, stop in stops_by_ticker.items():
        if ticker not in broker_tickers:
            broker_positions.append({
                "ticker": stop.ticker,
                "asset_type": stop.asset_type,
                "side": stop.side,
                "entry_price": stop.entry_price,
                "current_price": None,
                "quantity": stop.quantity,
                "market_value": stop.notional,
                "unrealized_pl": None,
                "pnl_pct": None,
                "stop_loss": stop.stop_loss,
                "take_profit": stop.take_profit,
                "high_water": stop.high_water,
                "trail_pct": stop.trail_pct,
                "order_id": stop.order_id,
                "opened_at": stop.opened_at,
                "source": "stop_monitor",
            })

    return broker_positions


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
