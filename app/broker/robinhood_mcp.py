"""Robinhood Agentic Trading MCP client.

Handles both DATA and EXECUTION through the official MCP endpoint.
Replaces Polygon.io for live market data and extends execution
beyond what Vibe-Trading SDK covers (options, crypto, prediction markets).

MCP endpoint: https://agent.robinhood.com/mcp/trading
Docs: https://robinhood.com/us/en/support/articles/agentic-trading/
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_BASE = "https://agent.robinhood.com/mcp/trading"
_TIMEOUT = 20


class RobinhoodMCP:
    """Thin client over the Robinhood Agentic Trading MCP REST interface."""

    def __init__(self) -> None:
        self._session_token: Optional[str] = None

    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._session_token:
            h["Authorization"] = f"Bearer {self._session_token}"
        return h

    def _call(self, tool: str, params: dict) -> Optional[dict]:
        """Call a single MCP tool and return the result dict."""
        try:
            r = httpx.post(
                f"{_BASE}/call",
                headers=self._headers(),
                json={"tool": tool, "params": params},
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("error"):
                log.warning("MCP tool=%s error: %s", tool, data["error"])
                return None
            return data.get("result")
        except Exception as exc:
            log.warning("MCP call failed tool=%s: %s", tool, exc)
            return None

    # ── Account ───────────────────────────────────────────────────────────────

    def get_account(self) -> Dict:
        """Buying power, portfolio value, cash."""
        result = self._call("get_account", {})
        return result or {}

    def get_positions(self) -> List[Dict]:
        """All current open positions across stocks, options, crypto."""
        result = self._call("get_positions", {})
        if not result:
            return []
        return result.get("positions", result) if isinstance(result, dict) else result

    def get_position(self, ticker: str) -> Optional[Dict]:
        """Get position for a specific ticker, or None if not held."""
        positions = self.get_positions()
        ticker_up = ticker.upper()
        for p in positions:
            sym = (p.get("symbol") or p.get("ticker") or "").upper()
            if sym == ticker_up:
                return p
        return None

    # ── Market data ───────────────────────────────────────────────────────────

    def get_quote(self, ticker: str, asset_type: str = "stock") -> Dict:
        """Live quote: price, bid, ask, volume, VWAP, change_pct."""
        tool_map = {
            "stock": "get_stock_quote",
            "crypto": "get_crypto_quote",
            "option": "get_option_quote",
            "prediction": "get_prediction_quote",
        }
        tool = tool_map.get(asset_type, "get_stock_quote")
        result = self._call(tool, {"symbol": ticker})
        return result or {}

    def get_options_chain(self, ticker: str, expiry: Optional[str] = None) -> List[Dict]:
        """Full options chain for a ticker. expiry format: YYYY-MM-DD."""
        params: dict = {"symbol": ticker}
        if expiry:
            params["expiration_date"] = expiry
        result = self._call("get_options_chain", params)
        if not result:
            return []
        return result.get("options", result) if isinstance(result, dict) else result

    def get_crypto_quote(self, symbol: str) -> Dict:
        """Live crypto price, 24h volume, funding rate."""
        result = self._call("get_crypto_quote", {"symbol": symbol})
        return result or {}

    def get_market_hours(self) -> Dict:
        """Is the market open right now? Pre-market / after-hours / closed."""
        result = self._call("get_market_hours", {})
        return result or {}

    def get_earnings_calendar(self, ticker: str) -> Optional[Dict]:
        """Next earnings date and estimate for a ticker."""
        result = self._call("get_earnings", {"symbol": ticker})
        return result

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_stock_order(
        self, ticker: str, side: str, notional: Optional[float] = None,
        quantity: Optional[float] = None, order_type: str = "market",
        extended_hours: bool = True,
    ) -> Dict:
        params = {
            "symbol": ticker.upper(),
            "side": side.lower(),
            "order_type": order_type,
            "extended_hours": extended_hours,
        }
        if notional:
            params["notional"] = notional
        elif quantity:
            params["quantity"] = quantity
        result = self._call("place_stock_order", params)
        return result or {"status": "error", "error": "No response from MCP"}

    def place_crypto_order(
        self, symbol: str, side: str, notional: Optional[float] = None,
        quantity: Optional[float] = None,
    ) -> Dict:
        params = {
            "symbol": symbol.upper(),
            "side": side.lower(),
        }
        if notional:
            params["notional"] = notional
        elif quantity:
            params["quantity"] = quantity
        result = self._call("place_crypto_order", params)
        return result or {"status": "error", "error": "No response from MCP"}

    def place_option_order(
        self, contract_id: str, side: str, quantity: int,
        order_type: str = "market",
    ) -> Dict:
        params = {
            "contract_id": contract_id,
            "side": side.lower(),
            "quantity": quantity,
            "order_type": order_type,
        }
        result = self._call("place_option_order", params)
        return result or {"status": "error", "error": "No response from MCP"}

    def cancel_order(self, order_id: str) -> bool:
        result = self._call("cancel_order", {"order_id": order_id})
        return bool(result)

    # ── Rich context for agents ───────────────────────────────────────────────

    def get_market_context(self, ticker: str, asset_type: str = "stock") -> Dict:
        """Full context dict fed to graph enrichment node."""
        quote = self.get_quote(ticker, asset_type)
        position = self.get_position(ticker) if asset_type != "crypto" else None
        account = self.get_account()
        hours = self.get_market_hours()

        price = quote.get("price") or quote.get("last_trade_price") or 0
        prev_close = quote.get("previous_close") or 0
        change_pct = round(((price - prev_close) / prev_close * 100), 2) if prev_close else 0
        volume = quote.get("volume") or quote.get("volume_24h") or 0
        vwap = quote.get("vwap") or 0

        return {
            "price": price,
            "change_pct": change_pct,
            "volume": volume,
            "vwap": vwap,
            "high": quote.get("high") or quote.get("high_price") or 0,
            "low": quote.get("low") or quote.get("low_price") or 0,
            "bid": quote.get("bid_price") or 0,
            "ask": quote.get("ask_price") or 0,
            "buying_power": account.get("buying_power") or account.get("cash") or 0,
            "already_holding": position is not None,
            "current_position": position,
            "market_session": hours.get("session") or "unknown",
            "market_open": hours.get("is_open", True),
            "extended_hours_open": hours.get("extended_hours_open", True),
        }


# Singleton
robinhood = RobinhoodMCP()
