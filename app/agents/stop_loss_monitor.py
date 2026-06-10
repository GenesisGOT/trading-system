"""Stop loss / take profit monitor — runs every 5 minutes, checks all open positions.

For every open position:
  - Checks current price vs entry price
  - Fires SELL if stop loss hit
  - Fires SELL if take profit hit
  - Sends Telegram alert with P&L

Stop levels are stored in SQLite when a position is opened.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.config import settings
from app.database import log_execution, log_system_event
from app.notifications import notify

log = logging.getLogger(__name__)


@dataclass
class StopLevel:
    ticker: str
    asset_type: str
    side: str           # buy | sell
    entry_price: float
    stop_loss: float    # price to exit at loss
    take_profit: float  # price to exit at gain
    quantity: float
    notional: Optional[float]
    order_id: Optional[str]
    opened_at: str


# In-memory store (persists as long as process is alive)
# In production this should be in SQLite — future improvement
_open_positions: Dict[str, StopLevel] = {}


def register_stop(
    ticker: str,
    asset_type: str,
    side: str,
    entry_price: float,
    quantity: float,
    notional: Optional[float],
    order_id: Optional[str],
    stop_loss_pct: float = 0.05,
    take_profit_pct: float = 0.10,
    atr_stop: Optional[float] = None,
) -> None:
    """Register a stop loss and take profit for a newly opened position."""
    if entry_price <= 0:
        return

    stop = atr_stop if atr_stop else round(entry_price * (1 - stop_loss_pct), 4)
    target = round(entry_price * (1 + take_profit_pct), 4)

    _open_positions[ticker] = StopLevel(
        ticker=ticker,
        asset_type=asset_type,
        side=side,
        entry_price=entry_price,
        stop_loss=stop,
        take_profit=target,
        quantity=quantity,
        notional=notional,
        order_id=order_id,
        opened_at=datetime.now(timezone.utc).isoformat(),
    )
    log.info("[%s] Stop registered: entry=%.4f stop=%.4f target=%.4f",
             ticker, entry_price, stop, target)
    notify(
        f"🎯 *{ticker}* position opened\n"
        f"Entry: ${entry_price:.2f}  Stop: ${stop:.2f} (-{stop_loss_pct:.0%})  "
        f"Target: ${target:.2f} (+{take_profit_pct:.0%})"
    )


def remove_stop(ticker: str) -> None:
    _open_positions.pop(ticker, None)


def get_open_stops() -> List[StopLevel]:
    return list(_open_positions.values())


async def check_stops() -> None:
    """Check all open positions against current prices. Called every 5 minutes."""
    if not _open_positions:
        return

    from app.broker.robinhood_mcp import robinhood
    from app.broker.connector import broker

    for ticker, pos in list(_open_positions.items()):
        try:
            ctx = robinhood.get_market_context(ticker, pos.asset_type)
            current_price = ctx.get("price", 0)
            if not current_price:
                continue

            pnl_pct = (current_price - pos.entry_price) / pos.entry_price
            hit_stop   = current_price <= pos.stop_loss
            hit_target = current_price >= pos.take_profit

            if not (hit_stop or hit_target):
                continue

            reason = "STOP LOSS" if hit_stop else "TAKE PROFIT"
            emoji  = "🛑" if hit_stop else "🎯"
            pnl_str = f"{pnl_pct:+.1%}"

            log.warning("[%s] %s hit — entry=%.4f current=%.4f pnl=%s",
                        ticker, reason, pos.entry_price, current_price, pnl_str)

            # Place exit order
            if not settings.dry_run:
                exit_side = "sell" if pos.side == "buy" else "buy"
                order = broker.place_order(
                    ticker, exit_side, pos.quantity, pos.notional,
                    asset_type=pos.asset_type,
                )
                order_id = order.order_id
                status = order.status
            else:
                order_id = None
                status = "dry_run"

            log_execution(
                decision_id=None,
                ticker=ticker,
                side="sell" if pos.side == "buy" else "buy",
                quantity=pos.quantity,
                notional_usd=pos.notional,
                order_id=order_id,
                status=status,
                block_reason=None,
                broker_response=None,
            )

            notify(
                f"{emoji} *{ticker}* — {reason}\n"
                f"Entry: ${pos.entry_price:.2f} → Exit: ${current_price:.2f} ({pnl_str})\n"
                f"{'Loss' if hit_stop else 'Profit'}: ${abs((current_price - pos.entry_price) * (pos.quantity or 1)):.2f}\n"
                f"Order: `{order_id or 'dry_run'}`"
            )

            remove_stop(ticker)

        except Exception as exc:
            log.error("[%s] Stop check failed: %s", ticker, exc)
