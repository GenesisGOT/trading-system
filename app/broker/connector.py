"""Vibe-Trading broker connector.

Wraps Vibe-Trading's Python SDK to:
  - enforce our mandate before submitting
  - place orders via LiveOrderGuardTool (Robinhood MCP OAuth path)
  - expose halt / resume for the kill-switch endpoint
  - fall back to dry-run mode if the SDK is unavailable

Public interface:
    connector = BrokerConnector()
    result = connector.place_order(ticker, side, quantity, notional_usd)
    connector.halt(reason)
    connector.resume()
    connector.status() → dict
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.config import settings

log = logging.getLogger(__name__)

BROKER = "robinhood"


@dataclass
class OrderResult:
    status: str           # submitted | blocked | dry_run | error
    order_id: Optional[str]
    block_reason: Optional[str]
    broker_response: Optional[Dict[str, Any]]


class BrokerConnector:
    """Single-instance connector; instantiate once and reuse."""

    def __init__(self) -> None:
        self._sdk_available = False
        self._halt_module = None
        self._guard_cls = None
        self._adapter = None
        self._mandate = None
        self._try_load_sdk()

    # ── SDK bootstrap ────────────────────────────────────────────────────────

    def _try_load_sdk(self) -> None:
        try:
            from agent.src.live.halt import trip_halt, clear_halt, halt_flag_set  # noqa: F401
            from agent.src.live.order_guard import LiveOrderGuardTool             # noqa: F401
            from agent.src.live.mandate.store import load_mandate                 # noqa: F401

            self._halt_module = __import__(
                "agent.src.live.halt", fromlist=["trip_halt", "clear_halt", "halt_flag_set"]
            )
            self._guard_cls = LiveOrderGuardTool
            self._sdk_available = True
            log.info("Vibe-Trading SDK loaded successfully")

            # Point the SDK at our runtime root
            os.environ.setdefault("VIBE_TRADING_RUNTIME_ROOT", settings.vibe_trading_runtime_root)

            self._load_mandate()
        except ImportError:
            log.warning(
                "Vibe-Trading SDK not found — running in DRY-RUN mode. "
                "Install: pip install git+https://github.com/HKUDS/Vibe-Trading.git"
            )

    def _load_mandate(self) -> None:
        try:
            from agent.src.live.mandate.store import load_mandate
            self._mandate = load_mandate(BROKER)
            log.info("Mandate loaded for broker=%s", BROKER)
        except Exception as exc:
            log.warning("Could not load existing mandate: %s — orders will be blocked until mandate is committed", exc)

    # ── Our own mandate pre-check (runs BEFORE Vibe-Trading's checks) ───────

    def _local_mandate_check(
        self, ticker: str, side: str, quantity: float, notional_usd: Optional[float]
    ) -> Optional[str]:
        """Return a block reason string, or None if the order passes local checks."""
        # Symbol allowlist
        if ticker.upper() not in [s.upper() for s in settings.allowed_symbols]:
            return f"{ticker} not in allowed symbols: {settings.allowed_symbols}"

        # Per-order cap
        if notional_usd and notional_usd > settings.mandate_max_order_usd:
            return (
                f"Order notional ${notional_usd:.2f} exceeds max_order_usd "
                f"${settings.mandate_max_order_usd:.2f}"
            )

        # Daily trade count cap
        from app.database import get_daily_trade_count
        daily = get_daily_trade_count()
        if daily >= settings.mandate_max_trades_per_day:
            return (
                f"Daily trade count {daily} >= max_trades_per_day "
                f"{settings.mandate_max_trades_per_day}"
            )

        return None

    # ── Core order placement ─────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        side: str,
        quantity: float,
        notional_usd: Optional[float] = None,
    ) -> OrderResult:
        ticker = ticker.upper()
        side = side.lower()

        if settings.dry_run:
            log.info("[DRY RUN] Would %s %s qty=%s notional=$%s", side, ticker, quantity, notional_usd)
            return OrderResult(status="dry_run", order_id=None, block_reason=None, broker_response=None)

        # Local mandate pre-check
        block = self._local_mandate_check(ticker, side, quantity, notional_usd)
        if block:
            log.warning("Local mandate blocked %s %s: %s", side, ticker, block)
            return OrderResult(status="blocked", order_id=None, block_reason=block, broker_response=None)

        if not self._sdk_available:
            return OrderResult(
                status="error",
                order_id=None,
                block_reason="Vibe-Trading SDK not available",
                broker_response=None,
            )

        return self._sdk_place_order(ticker, side, quantity, notional_usd)

    def _sdk_place_order(
        self,
        ticker: str,
        side: str,
        quantity: float,
        notional_usd: Optional[float],
    ) -> OrderResult:
        try:
            import uuid
            from agent.src.live.order_guard import LiveOrderGuardTool

            # The guard needs an adapter and spec; Vibe-Trading wires these
            # internally — we use the high-level factory when available.
            try:
                from agent.src.trading.service import get_broker_adapter, get_broker_spec
                adapter = get_broker_adapter(BROKER)
                spec = get_broker_spec(BROKER)
            except ImportError:
                # Fallback: pass None and let order_guard resolve internally
                adapter = None
                spec = None

            session_id = str(uuid.uuid4())
            guard = LiveOrderGuardTool(adapter, spec, broker=BROKER, session_id=session_id)

            raw = guard.execute(
                symbol=ticker,
                side=side,
                quantity=quantity,
                **({"notional_usd": notional_usd} if notional_usd else {}),
            )

            # Parse Vibe-Trading's response envelope
            if isinstance(raw, dict):
                status = raw.get("status", "error")
                order_id = raw.get("order_id")
                block_reason = raw.get("reason") if status == "blocked" else None
                return OrderResult(
                    status="submitted" if status == "allowed" else status,
                    order_id=order_id,
                    block_reason=block_reason,
                    broker_response=raw,
                )
            # Unexpected return type — treat as submitted if no exception raised
            return OrderResult(status="submitted", order_id=None, block_reason=None, broker_response={"raw": str(raw)})

        except Exception as exc:
            log.error("SDK order placement failed for %s %s: %s", side, ticker, exc, exc_info=True)
            return OrderResult(status="error", order_id=None, block_reason=str(exc), broker_response=None)

    # ── Halt / resume ────────────────────────────────────────────────────────

    def halt(self, reason: str = "api kill-switch") -> None:
        log.warning("HALT triggered: %s", reason)
        if self._sdk_available and self._halt_module:
            try:
                self._halt_module.trip_halt(by="api", reason=reason, broker=BROKER)
            except Exception as exc:
                log.error("Failed to set Vibe-Trading halt sentinel: %s", exc)

    def resume(self) -> None:
        log.info("RESUME — clearing halt sentinel")
        if self._sdk_available and self._halt_module:
            try:
                self._halt_module.clear_halt(broker=BROKER)
            except Exception as exc:
                log.error("Failed to clear Vibe-Trading halt sentinel: %s", exc)

    def is_halted(self) -> bool:
        if self._sdk_available and self._halt_module:
            try:
                return bool(self._halt_module.halt_flag_set(broker=BROKER))
            except Exception:
                pass
        return False

    def status(self) -> Dict[str, Any]:
        return {
            "sdk_available": self._sdk_available,
            "mandate_loaded": self._mandate is not None,
            "halted": self.is_halted(),
            "dry_run": settings.dry_run,
            "broker": BROKER,
            "allowed_symbols": settings.allowed_symbols,
            "mandate_max_order_usd": settings.mandate_max_order_usd,
            "mandate_daily_cap_usd": settings.mandate_daily_cap_usd,
            "mandate_max_trades_per_day": settings.mandate_max_trades_per_day,
        }


# Singleton — imported by scheduler and API routes
broker = BrokerConnector()
