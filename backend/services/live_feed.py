"""
live_feed.py
============
WebSocket price broadcaster.

Polls yfinance every 5 seconds for all watchlist symbols and pushes
{symbol: price} JSON to every connected client.

Usage (FastAPI):
    app.add_api_websocket_route("/api/ws/prices", price_broadcast)
"""

from __future__ import annotations

import asyncio
import json
import logging

import yfinance as yf
from fastapi import WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from db.core import get_engine
from models.models import WatchlistItem

log = logging.getLogger(__name__)

POLL_INTERVAL = 5  # seconds


async def _fetch_prices(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    prices: dict[str, float] = {}
    try:
        tickers = yf.Tickers(" ".join(symbols))
        for sym in symbols:
            try:
                price = tickers.tickers[sym].fast_info.get("lastPrice")
                if price is not None:
                    prices[sym] = float(price)
            except Exception:
                pass
    except Exception as exc:
        log.warning("live_feed price fetch failed: %s", exc)
    return prices


def _get_watchlist_symbols() -> list[str]:
    with Session(get_engine()) as session:
        return list(session.exec(select(WatchlistItem.ticker).distinct()).all())


async def price_broadcast(websocket: WebSocket) -> None:
    """
    WebSocket handler — call directly as the route handler.
    Sends {prices: {TICKER: price, ...}} every POLL_INTERVAL seconds.
    """
    await websocket.accept()
    log.info("WS client connected: %s", websocket.client)
    try:
        while True:
            symbols = await asyncio.to_thread(_get_watchlist_symbols)
            prices = await _fetch_prices(symbols)
            await websocket.send_text(json.dumps({"prices": prices}))
            await asyncio.sleep(POLL_INTERVAL)
    except WebSocketDisconnect:
        log.info("WS client disconnected: %s", websocket.client)
    except Exception as exc:
        log.warning("WS error: %s", exc)
        try:
            await websocket.close()
        except Exception:
            pass
