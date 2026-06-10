"""Alpaca broker connector — paper trading and live trading.

Set env vars:
  ALPACA_API_KEY     — your Alpaca API key ID
  ALPACA_API_SECRET  — your Alpaca secret key
  ALPACA_PAPER=true  — paper trading (default); false = live

Paper endpoint: https://paper-api.alpaca.markets/v2
Live endpoint:  https://api.alpaca.markets/v2

Switch between paper and live by changing ALPACA_PAPER env var only.
No code changes needed.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

PAPER_URL = "https://paper-api.alpaca.markets/v2"
LIVE_URL  = "https://api.alpaca.markets/v2"


def _base_url() -> str:
    paper = getattr(settings, "alpaca_paper", True)
    if isinstance(paper, str):
        paper = paper.lower() != "false"
    return PAPER_URL if paper else LIVE_URL


def _headers() -> dict:
    key    = getattr(settings, "alpaca_api_key", None)
    secret = getattr(settings, "alpaca_api_secret", None)
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_API_SECRET must be set")
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Content-Type": "application/json",
    }


# ── Account & positions ───────────────────────────────────────────────────────

def get_account() -> dict:
    import httpx
    r = httpx.get(f"{_base_url()}/account", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_positions() -> list:
    import httpx
    r = httpx.get(f"{_base_url()}/positions", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_position(ticker: str) -> Optional[dict]:
    import httpx
    try:
        r = httpx.get(f"{_base_url()}/positions/{ticker}", headers=_headers(), timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ── Market data ───────────────────────────────────────────────────────────────

def get_quote(ticker: str) -> dict:
    """Get latest quote for a stock ticker."""
    import httpx
    r = httpx.get(
        f"https://data.alpaca.markets/v2/stocks/{ticker}/quotes/latest",
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("quote", {})


def get_latest_trade(ticker: str) -> dict:
    import httpx
    r = httpx.get(
        f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest",
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("trade", {})


def get_market_context(ticker: str, asset_type: str = "stock") -> dict:
    """Unified context dict matching the Robinhood MCP format."""
    try:
        account = get_account()
        buying_power = float(account.get("buying_power", 0))
        equity       = float(account.get("equity", 0))
        prev_equity  = float(account.get("last_equity", equity))

        position = get_position(ticker)

        price, change_pct, volume = 0.0, 0.0, 0
        try:
            trade = get_latest_trade(ticker)
            price = float(trade.get("p", 0))
            quote = get_quote(ticker)
            ask   = float(quote.get("ap", price))
            bid   = float(quote.get("bp", price))
        except Exception:
            pass

        return {
            "price": price,
            "change_pct": change_pct,
            "volume": volume,
            "vwap": 0,
            "high": 0,
            "low": 0,
            "bid": bid if "bid" in dir() else 0,
            "ask": ask if "ask" in dir() else 0,
            "buying_power": buying_power,
            "equity": equity,
            "prev_equity": prev_equity,
            "already_holding": position is not None,
            "current_position": position,
            "quantity": float(position.get("qty", 0)) if position else 0,
            "avg_entry_price": float(position.get("avg_entry_price", 0)) if position else 0,
            "unrealized_pnl": float(position.get("unrealized_pl", 0)) if position else 0,
            "market_session": "regular",
            "market_open": True,
            "extended_hours_open": True,
            "broker": "alpaca_paper" if getattr(settings, "alpaca_paper", True) else "alpaca_live",
        }
    except Exception as exc:
        log.warning("[%s] Alpaca market context failed: %s", ticker, exc)
        return {"price": 0, "buying_power": 0, "already_holding": False, "broker": "alpaca"}


# ── Order placement ───────────────────────────────────────────────────────────

def place_order(
    ticker: str,
    side: str,            # buy | sell
    quantity: float = 0,
    notional: Optional[float] = None,
    asset_type: str = "stock",
    order_type: str = "market",
    time_in_force: str = "day",
) -> dict:
    """Place a market order. Returns order dict with id and status."""
    import httpx

    body: dict = {
        "symbol": ticker,
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
        "extended_hours": False,
    }

    if notional and notional > 0:
        body["notional"] = str(round(notional, 2))
    elif quantity and quantity > 0:
        body["qty"] = str(quantity)
    else:
        body["notional"] = "100"   # fallback $100

    log.info("[%s] Alpaca order: side=%s notional=%s qty=%s", ticker, side, notional, quantity)

    r = httpx.post(
        f"{_base_url()}/orders",
        headers=_headers(),
        json=body,
        timeout=15,
    )

    if r.status_code in (200, 201):
        order = r.json()
        log.info("[%s] Order placed: id=%s status=%s", ticker, order.get("id"), order.get("status"))
        return {
            "order_id": order.get("id"),
            "status": "submitted",
            "block_reason": None,
            "broker_response": order,
        }
    else:
        err = r.text[:200]
        log.error("[%s] Alpaca order failed %d: %s", ticker, r.status_code, err)
        return {
            "order_id": None,
            "status": "error",
            "block_reason": f"Alpaca {r.status_code}: {err}",
            "broker_response": None,
        }


def cancel_order(order_id: str) -> bool:
    import httpx
    try:
        r = httpx.delete(f"{_base_url()}/orders/{order_id}", headers=_headers(), timeout=10)
        return r.status_code in (200, 204)
    except Exception:
        return False


def get_orders(status: str = "open", limit: int = 20) -> list:
    import httpx
    r = httpx.get(
        f"{_base_url()}/orders",
        headers=_headers(),
        params={"status": status, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ── Portfolio summary ─────────────────────────────────────────────────────────

def get_portfolio_summary() -> dict:
    """Return a clean portfolio summary for the dashboard."""
    try:
        account   = get_account()
        positions = get_positions()
        orders    = get_orders(status="open")
        equity    = float(account.get("equity", 0))
        cash      = float(account.get("cash", 0))
        prev_eq   = float(account.get("last_equity", equity))
        day_pnl   = equity - prev_eq
        day_pnl_pct = day_pnl / prev_eq if prev_eq else 0

        return {
            "equity": equity,
            "cash": cash,
            "buying_power": float(account.get("buying_power", 0)),
            "day_pnl": day_pnl,
            "day_pnl_pct": day_pnl_pct,
            "open_positions": len(positions),
            "open_orders": len(orders),
            "positions": positions,
            "account_status": account.get("status"),
            "pattern_day_trader": account.get("pattern_day_trader", False),
            "is_paper": getattr(settings, "alpaca_paper", True),
        }
    except Exception as exc:
        log.error("Alpaca portfolio summary failed: %s", exc)
        return {}
