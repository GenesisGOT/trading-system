"""
Stock Research API — FastAPI backend
All data sourced from free/open libraries (yfinance, feedparser)
Extended with Robinhood integration, trade execution, sleeve allocation, and predictions.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import yfinance as yf
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from config import get_settings
from db.core import init_and_migrate_db
from routers import alerts, auth, earnings, news, profile, projection, screener, stock
from routers.robinhood import router as robinhood_router
from routers.trades import router as trades_router
from routers.allocation import router as allocation_router, rebalance_router
from routers.predictions import router as predictions_router
from services.scheduler import check_prices_and_notify, check_allocation_and_notify, notify_earnings_summary, warm_dashboard_cache
from services.live_feed import price_broadcast

VERSION = "1.4.0"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def _silence_http_logging():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _ensure_new_tables():
    """Create new tables added by this extension without running full Alembic migration."""
    from sqlmodel import SQLModel
    from db.core import get_engine
    import models.trade_models  # noqa: F401 — registers models with SQLModel metadata
    SQLModel.metadata.create_all(get_engine())


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(get_settings().FRONTEND_FOLDER).mkdir(parents=True, exist_ok=True)
    log.info("Initializing database...")
    yf.set_tz_cache_location(str(Path(get_settings().YF_CACHE_PATH).parent))
    await init_and_migrate_db()
    _ensure_new_tables()
    _silence_http_logging()
    log.info("STONKS ready")
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices_and_notify, 'cron', day_of_week='mon-fri', hour='7-22', minute='*/15')
    scheduler.add_job(notify_earnings_summary, 'cron', day_of_week='mon-fri', hour=8, minute=0)
    scheduler.add_job(warm_dashboard_cache,        'cron', day_of_week='mon-fri', hour='7-22', minute='*/45')
    scheduler.add_job(check_allocation_and_notify, 'cron', day_of_week='mon-fri', hour='7-22', minute='*/15')
    scheduler.start()
    log.info("Scheduler (alerts + earnings notifier) ready")
    yield
    scheduler.shutdown()
    log.info("Shutting down")

app = FastAPI(title="Stonks", version=VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(GZipMiddleware, minimum_size=1000)

# Original stonks routers
app.include_router(auth.router, prefix="/api")
app.include_router(stock.router, prefix="/api")
app.include_router(news.router, prefix="/api")
app.include_router(profile.router, prefix="/api")
app.include_router(alerts.router, prefix="/api")
app.include_router(projection.router, prefix="/api")
app.include_router(earnings.router, prefix="/api")
app.include_router(screener.router, prefix="/api")

# Extended routers
app.include_router(robinhood_router, prefix="/api")
app.include_router(trades_router, prefix="/api")
app.include_router(allocation_router, prefix="/api")
app.include_router(rebalance_router, prefix="/api")
app.include_router(predictions_router, prefix="/api")


@app.get("/api/")
def info():
    return {"version": VERSION}


@app.websocket("/api/ws/prices")
async def ws_prices(websocket: WebSocket):
    """Live price WebSocket — streams {prices: {TICKER: price}} every 5s."""
    await price_broadcast(websocket)


@app.middleware("http")
async def not_found_to_spa(request: Request, call_next):
    response = await call_next(request)
    if response.status_code == 404 and not request.url.path.startswith(("/api", "/assets")):
        return FileResponse(Path(get_settings().FRONTEND_FOLDER) / "index.html")
    return response


app.mount("/", StaticFiles(directory=get_settings().FRONTEND_FOLDER, html=True), name="frontend")
