"""
trade_service.py
================
Trade execution via Robinhood MCP (official agentic API).

Order flow:
  POST /api/trades/place  →  trade_service.place_order()
                          →  RobinhoodMCP  →  Robinhood
                          →  logs row to trade_executions table

DRY_RUN mode: set DRY_RUN=true in config.env to log orders without sending.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import httpx
from sqlmodel import Session

from db.core import get_engine
from models.trade_models import TradeExecution

log = logging.getLogger(__name__)

MCP_BASE = "https://agent.robinhood.com/mcp/trading"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"


def _mcp_headers() -> dict:
    token = os.getenv("ROBINHOOD_MCP_TOKEN", "").strip()
    if not token:
        raise ValueError("ROBINHOOD_MCP_TOKEN not set in config.env")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def place_order(
    ticker: str,
    side: str,
    quantity: float,
    order_type: str = "market",
    asset_type: str = "stock",
    limit_price: float | None = None,
    username: str = "system",
) -> dict:
    """
    Place an order via RobinhoodMCP and persist the result.

    Returns the persisted TradeExecution as a dict.
    """
    ticker = ticker.upper().strip()
    side = side.lower()
    order_type = order_type.lower()

    status = "dry_run"
    order_id = None
    error_msg = None

    if not DRY_RUN:
        try:
            payload: dict = {
                "symbol": ticker,
                "side": side,
                "type": order_type,
                "quantity": quantity,
                "asset_type": asset_type,
            }
            if order_type == "limit" and limit_price:
                payload["limit_price"] = limit_price

            resp = httpx.post(
                f"{MCP_BASE}/orders",
                json=payload,
                headers=_mcp_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            order_id = data.get("id") or data.get("order_id")
            status = "submitted"
        except Exception as exc:
            log.error("Trade placement failed [%s %s %s]: %s", side, quantity, ticker, exc)
            error_msg = str(exc)
            status = "error"

    # Persist
    with Session(get_engine()) as session:
        row = TradeExecution(
            username=username,
            ticker=ticker,
            side=side,
            quantity=quantity,
            order_type=order_type,
            asset_type=asset_type,
            limit_price=limit_price,
            order_id=order_id,
            status=status,
            error_msg=error_msg,
            created_at=datetime.now(UTC),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return {
            "id": row.id,
            "ticker": row.ticker,
            "side": row.side,
            "quantity": row.quantity,
            "order_type": row.order_type,
            "asset_type": row.asset_type,
            "order_id": row.order_id,
            "status": row.status,
            "error_msg": row.error_msg,
            "created_at": row.created_at.isoformat(),
        }
