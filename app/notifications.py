"""Push notifications — Discord webhook and/or Telegram bot."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)


def _discord(message: str) -> None:
    if not settings.discord_webhook_url:
        return
    try:
        r = httpx.post(
            settings.discord_webhook_url,
            json={"content": message},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as exc:
        log.warning("Discord notification failed: %s", exc)


def _telegram(message: str) -> None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    try:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        r = httpx.post(
            url,
            json={"chat_id": settings.telegram_chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as exc:
        log.warning("Telegram notification failed: %s", exc)


def notify(message: str) -> None:
    """Send message to all configured notification channels (best-effort)."""
    log.info("NOTIFY: %s", message)
    _discord(message)
    _telegram(message)


def notify_trade(
    ticker: str,
    side: str,
    quantity: float,
    notional: Optional[float],
    status: str,
    order_id: Optional[str],
    reason: Optional[str],
    rating: str,
) -> None:
    side_emoji = "🟢" if side == "buy" else "🔴"
    notional_str = f"${notional:,.2f}" if notional else "?"
    order_str = f"`{order_id}`" if order_id else "n/a"
    block_str = f"\n⚠️ Blocked: {reason}" if reason else ""
    msg = (
        f"{side_emoji} **TRADE {status.upper()}** — {ticker}\n"
        f"Side: {side.upper()} | Qty: {quantity} | Notional: {notional_str}\n"
        f"Agent rating: {rating} | Order ID: {order_str}{block_str}"
    )
    notify(msg)


def notify_halt(reason: str) -> None:
    notify(f"🛑 **TRADING HALTED** — {reason}")


def notify_resume() -> None:
    notify("▶️ **Trading resumed**")


def notify_error(ticker: str, error: str) -> None:
    notify(f"❌ **ERROR** on {ticker}: {error[:200]}")
