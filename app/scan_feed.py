"""Live scan feed — broadcast real-time scan events to SSE subscribers."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

# All active SSE subscriber queues
_subscribers: List[asyncio.Queue] = []


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


async def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    try:
        _subscribers.remove(q)
    except ValueError:
        pass


def emit(event_type: str, data: Dict[str, Any]) -> None:
    """Emit a scan event to all connected dashboard subscribers."""
    payload = {"type": event_type, "ts": _now(), **data}
    msg = f"data: {json.dumps(payload)}\n\n"
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        unsubscribe(q)


# ── Convenience emitters ──────────────────────────────────────────────────────

def emit_scan_start(run_id: str, tickers: list, session: str) -> None:
    emit("scan_start", {"run_id": run_id, "tickers": tickers, "session": session})


def emit_ticker_start(run_id: str, ticker: str, asset_type: str) -> None:
    emit("ticker_start", {"run_id": run_id, "ticker": ticker, "asset_type": asset_type})


def emit_analyst_update(run_id: str, ticker: str, analyst: str, status: str) -> None:
    emit("analyst", {"run_id": run_id, "ticker": ticker, "analyst": analyst, "status": status})


def emit_decision(run_id: str, ticker: str, action: str, confidence: float, rating: str, thesis: str) -> None:
    emit("decision", {
        "run_id": run_id, "ticker": ticker, "action": action,
        "confidence": round(confidence, 2), "rating": rating,
        "thesis": thesis[:200],
    })


def emit_scan_complete(run_id: str, count: int) -> None:
    emit("scan_complete", {"run_id": run_id, "count": count})


def emit_category_start(run_id: str, category: str, tickers: list) -> None:
    emit("category_start", {"run_id": run_id, "category": category, "tickers": tickers})


def emit_category_complete(run_id: str, category: str, count: int) -> None:
    emit("category_complete", {"run_id": run_id, "category": category, "count": count})


def emit_error(ticker: str, error: str) -> None:
    emit("error", {"ticker": ticker, "error": str(error)[:200]})
