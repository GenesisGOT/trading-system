"""Stop loss / take profit monitor — runs every 5 minutes, checks all open positions.

Trailing stop logic:
  - Tracks the highest price seen since entry (high-water mark)
  - Every cycle: if price > high_water, ratchet stop up to preserve gains
  - Stop never moves DOWN — only follows the trend up
  - Uses ATR-based trail distance (dynamic) or a fixed trail % fallback

Example: BUY at $100, trail=2%
  Price hits $110 → stop moves to $107.80 (locks in +7.8%)
  Price hits $120 → stop moves to $117.60 (locks in +17.6%)
  Price drops to $117 → EXIT at profit, not original stop
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.config import settings
from app.database import log_execution, log_system_event, save_position, delete_position, load_positions
from app.notifications import notify

log = logging.getLogger(__name__)

# Default trail distance as % of price (used when ATR unavailable)
DEFAULT_TRAIL_PCT = 0.03   # 3%
# Minimum trail distance — never let stop get closer than 1% to current price
MIN_TRAIL_PCT = 0.01


@dataclass
class StopLevel:
    ticker: str
    asset_type: str
    side: str               # buy | sell
    entry_price: float
    stop_loss: float        # current trailing stop price (moves up with price)
    take_profit: float      # hard take-profit target (optional ceiling)
    quantity: float
    notional: Optional[float]
    order_id: Optional[str]
    opened_at: str
    high_water: float = 0.0        # highest price seen since entry
    trail_distance: float = 0.0    # $ distance to keep stop below high-water
    trail_pct: float = DEFAULT_TRAIL_PCT  # % trail used when recalculating


# In-memory store (persists as long as process is alive)
_open_positions: Dict[str, StopLevel] = {}


def _get_trail_distance(ticker: str, asset_type: str, entry_price: float) -> float:
    """Compute ATR-based trail distance. Falls back to DEFAULT_TRAIL_PCT * entry."""
    try:
        from app.agents.tools.indicators import suggested_stop_loss
        atr_stop = suggested_stop_loss(ticker, entry_price, asset_type)
        if atr_stop and atr_stop > 0:
            dist = entry_price - atr_stop
            # Clamp: trail must be at least 1% and at most 8% of entry
            dist = max(dist, entry_price * MIN_TRAIL_PCT)
            dist = min(dist, entry_price * 0.08)
            return round(dist, 4)
    except Exception:
        pass
    return round(entry_price * DEFAULT_TRAIL_PCT, 4)


def register_stop(
    ticker: str,
    asset_type: str,
    side: str,
    entry_price: float,
    quantity: float,
    notional: Optional[float],
    order_id: Optional[str],
    stop_loss_pct: float = 0.05,
    take_profit_pct: float = 0.15,
    atr_stop: Optional[float] = None,
) -> None:
    """Register a trailing stop for a newly opened position."""
    if entry_price <= 0:
        return

    # Compute trail distance (ATR-based)
    trail_dist = _get_trail_distance(ticker, asset_type, entry_price)
    trail_pct = trail_dist / entry_price

    # Initial stop: use explicit atr_stop if provided, else trail below entry
    initial_stop = atr_stop if (atr_stop and atr_stop > 0) else round(entry_price - trail_dist, 4)
    # Hard take-profit ceiling (can be disabled by setting very high)
    hard_target = round(entry_price * (1 + take_profit_pct), 4)

    pos = StopLevel(
        ticker=ticker,
        asset_type=asset_type,
        side=side,
        entry_price=entry_price,
        stop_loss=initial_stop,
        take_profit=hard_target,
        quantity=quantity,
        notional=notional,
        order_id=order_id,
        opened_at=datetime.now(timezone.utc).isoformat(),
        high_water=entry_price,
        trail_distance=trail_dist,
        trail_pct=trail_pct,
    )
    _open_positions[ticker] = pos
    try:
        save_position(pos)
    except Exception as exc:
        log.warning("[%s] Failed to persist position to DB: %s", ticker, exc)
    log.info(
        "[%s] Trailing stop registered: entry=%.4f initial_stop=%.4f trail=%.1f%% target=%.4f",
        ticker, entry_price, initial_stop, trail_pct * 100, hard_target,
    )
    notify(
        f"🎯 *{ticker}* position opened\n"
        f"Entry: ${entry_price:.2f}\n"
        f"Trailing stop: ${initial_stop:.2f} ({trail_pct:.1%} trail)\n"
        f"Take-profit target: ${hard_target:.2f} (+{take_profit_pct:.0%})"
    )


def remove_stop(ticker: str) -> None:
    _open_positions.pop(ticker, None)
    try:
        delete_position(ticker)
    except Exception:
        pass


def restore_positions_from_db() -> None:
    """Called on startup — reload open positions from SQLite so trailing stops survive redeploys."""
    try:
        rows = load_positions()
        for r in rows:
            pos = StopLevel(
                ticker=r["ticker"],
                asset_type=r["asset_type"],
                side=r["side"],
                entry_price=r["entry_price"],
                stop_loss=r["stop_loss"],
                take_profit=r["take_profit"],
                quantity=r["quantity"],
                notional=r["notional"],
                order_id=r["order_id"],
                opened_at=r["opened_at"],
                high_water=r["high_water"],
                trail_distance=r["trail_distance"],
                trail_pct=r["trail_pct"],
            )
            _open_positions[r["ticker"]] = pos
        if rows:
            log.info("Restored %d open positions from DB", len(rows))
    except Exception as exc:
        log.warning("Could not restore positions from DB: %s", exc)


def get_open_stops() -> List[StopLevel]:
    return list(_open_positions.values())


def _ratchet_stop(pos: StopLevel, current_price: float) -> bool:
    """Move stop up if price made a new high. Returns True if stop was moved."""
    if current_price <= pos.high_water:
        return False

    new_stop = round(current_price - pos.trail_distance, 4)
    # Only move stop if it would go higher than current stop
    if new_stop <= pos.stop_loss:
        return False

    old_stop = pos.stop_loss
    pos.high_water = current_price
    pos.stop_loss = new_stop
    locked_pct = (new_stop - pos.entry_price) / pos.entry_price

    log.info(
        "[%s] Trailing stop ratcheted: price=%.4f stop %.4f → %.4f (locked %+.1f%%)",
        pos.ticker, current_price, old_stop, new_stop, locked_pct * 100,
    )
    try:
        save_position(pos)
    except Exception:
        pass
    return True


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

            # ── Ratchet stop up if new high ───────────────────────────────
            stop_moved = _ratchet_stop(pos, current_price)

            pnl_pct = (current_price - pos.entry_price) / pos.entry_price
            hit_stop   = current_price <= pos.stop_loss
            hit_target = current_price >= pos.take_profit

            # Send trail update notification (every time stop moves)
            if stop_moved and not hit_stop and not hit_target:
                locked = (pos.stop_loss - pos.entry_price) / pos.entry_price
                notify(
                    f"📈 *{ticker}* trailing stop moved up\n"
                    f"Price: ${current_price:.2f}  New stop: ${pos.stop_loss:.2f}\n"
                    f"Locked in: {locked:+.1%} above entry"
                )

            if not (hit_stop or hit_target):
                continue

            reason = "TRAILING STOP" if hit_stop else "TAKE PROFIT"
            emoji  = "🛑" if hit_stop else "🎯"
            pnl_str = f"{pnl_pct:+.1%}"

            log.warning(
                "[%s] %s triggered — entry=%.4f high=%.4f exit=%.4f pnl=%s",
                ticker, reason, pos.entry_price, pos.high_water, current_price, pnl_str,
            )

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

            peak_gain = (pos.high_water - pos.entry_price) / pos.entry_price
            notify(
                f"{emoji} *{ticker}* — {reason}\n"
                f"Entry: ${pos.entry_price:.2f} → Peak: ${pos.high_water:.2f} ({peak_gain:+.1%})\n"
                f"Exit: ${current_price:.2f}  P&L: *{pnl_str}*\n"
                f"{'Loss' if pnl_pct < 0 else 'Profit'}: "
                f"${abs((current_price - pos.entry_price) * (pos.quantity or 1)):.2f}\n"
                f"Order: `{order_id or 'dry_run'}`"
            )

            remove_stop(ticker)

        except Exception as exc:
            log.error("[%s] Stop check failed: %s", ticker, exc)
