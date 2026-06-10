"""FastAPI application — entry point for the cloud-hosted trading system."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import settings
from app.database import (
    get_recent_decisions,
    get_recent_executions,
    get_daily_trade_count,
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

    from app.scheduler import start_scheduler
    start_scheduler()

    yield

    from app.scheduler import stop_scheduler
    stop_scheduler()
    log.info("Trading system shut down")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Multi-Agent Trading System",
    version="1.0.0",
    description="TradingAgents × Vibe-Trading × Robinhood MCP",
    lifespan=lifespan,
)

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
