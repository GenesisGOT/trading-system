"""Mem0 persistent agent memory — agents remember past decisions and outcomes."""
from __future__ import annotations

import logging
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

_mem0_client = None


def _client():
    global _mem0_client
    if _mem0_client is not None:
        return _mem0_client
    try:
        from mem0 import MemoryClient
        if settings.mem0_api_key:
            _mem0_client = MemoryClient(api_key=settings.mem0_api_key)
            return _mem0_client
    except ImportError:
        pass
    return None


def recall_ticker(ticker: str) -> str:
    """Retrieve past decisions and outcomes for a ticker."""
    client = _client()
    if not client:
        return ""
    try:
        results = client.search(
            query=f"{ticker} trading decision outcome",
            filters={"user_id": "trading_system"},
            limit=5,
        )
        if not results:
            return ""
        lines = [f"PAST DECISIONS FOR {ticker}:"]
        for r in results:
            # Mem0 v3 may return strings or dicts depending on SDK version
            mem = r if isinstance(r, str) else r.get("memory", r.get("text", ""))
            if mem:
                lines.append(f"• {mem}")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("[%s] Mem0 recall failed: %s", ticker, exc)
        return ""


def store_decision(ticker: str, action: str, confidence: float, thesis: str, outcome: Optional[str] = None) -> None:
    """Store a decision so future cycles can learn from it."""
    client = _client()
    if not client:
        return
    try:
        text = (
            f"On {ticker}: decided {action} with {confidence:.0%} confidence. "
            f"Thesis: {thesis[:200]}"
        )
        if outcome:
            text += f" Outcome: {outcome}"
        client.add(
            messages=[{"role": "assistant", "content": text}],
            user_id="trading_system",
            metadata={"ticker": ticker, "action": action},
        )
        log.debug("[%s] Stored decision in Mem0", ticker)
    except Exception as exc:
        log.warning("[%s] Mem0 store failed: %s", ticker, exc)


def recall_macro_lessons() -> str:
    """Retrieve past macro/market observations the system has learned."""
    client = _client()
    if not client:
        return ""
    try:
        results = client.search(
            query="market macro lesson learned mistake",
            filters={"user_id": "trading_system"},
            limit=3,
        )
        if not results:
            return ""
        def _extract(r):
            if isinstance(r, str):
                return r
            return r.get("memory", r.get("text", ""))
        return "PAST MARKET LESSONS:\n" + "\n".join(
            f"• {_extract(r)}" for r in results if _extract(r)
        )
    except Exception as exc:
        log.warning("Mem0 macro recall failed: %s", exc)
        return ""
