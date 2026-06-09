"""Push notifications — Discord webhook and Telegram bot.

Every trade notification includes the full agent reasoning:
  ticker, action, rating, confidence, investment thesis,
  key analyst signals, price target, validator notes,
  and execution status.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)


# ── Transport ────────────────────────────────────────────────────────────────

def _discord(message: str) -> None:
    if not settings.discord_webhook_url:
        return
    try:
        httpx.post(
            settings.discord_webhook_url,
            json={"content": message[:2000]},
            timeout=10,
        ).raise_for_status()
    except Exception as exc:
        log.warning("Discord notification failed: %s", exc)


def _telegram(message: str) -> None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": settings.telegram_chat_id,
                "text": message[:4096],
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        ).raise_for_status()
    except Exception as exc:
        log.warning("Telegram notification failed: %s", exc)


def notify(message: str) -> None:
    """Send to all configured channels (best-effort)."""
    log.info("NOTIFY: %s", message[:200])
    _discord(message)
    _telegram(message)


# ── Rich trade notification ───────────────────────────────────────────────────

def notify_trade(
    ticker: str,
    side: str,
    quantity: float,
    notional: Optional[float],
    status: str,
    order_id: Optional[str],
    block_reason: Optional[str],
    # Agent reasoning
    rating: str,
    confidence: Optional[float],
    investment_thesis: str,
    price_target: Optional[float],
    time_horizon: Optional[str],
    risk_summary: Optional[str],
    # Market context
    current_price: Optional[float] = None,
    change_pct: Optional[float] = None,
    volume: Optional[int] = None,
    # Analyst highlights
    analyst_reports: Optional[dict] = None,
    asset_type: str = "stock",
) -> None:
    asset_emoji = {"stock": "📈", "option": "⚙️", "crypto": "₿", "prediction": "🎯"}.get(asset_type, "📈")
    side_emoji = "🟢" if side == "buy" else "🔴"
    status_emoji = {"submitted": "✅", "blocked": "🚫", "dry_run": "🧪", "error": "❌"}.get(status, "⚠️")

    notional_str = f"${notional:,.2f}" if notional else "n/a"
    price_str = f"${current_price:.2f}" if current_price else "n/a"
    change_str = f"{change_pct:+.1f}%" if change_pct is not None else ""
    vol_str = f"{volume / 1_000_000:.1f}M" if volume else "n/a"
    conf_str = f"{confidence * 100:.0f}%" if confidence is not None else "n/a"
    target_str = f"${price_target:.2f}" if price_target else "n/a"
    horizon_str = time_horizon or "n/a"
    order_str = f"`{order_id}`" if order_id else "n/a"

    # Truncate thesis to keep message clean
    thesis = (investment_thesis[:300] + "...") if len(investment_thesis) > 300 else investment_thesis
    risks = (risk_summary[:200] + "...") if risk_summary and len(risk_summary) > 200 else (risk_summary or "")

    # Build analyst highlights (one line each)
    analyst_lines = ""
    if analyst_reports:
        for name, report in list(analyst_reports.items())[:4]:
            if report:
                first_line = report.strip().split("\n")[0][:80]
                analyst_lines += f"\n  • *{name.title()}*: {first_line}"

    msg = (
        f"{side_emoji} {asset_emoji} *{ticker}* — {side.upper()} {status_emoji}{status.upper()}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Price:* {price_str} ({change_str})   📊 *Vol:* {vol_str}\n"
        f"📋 *Rating:* {rating}   🎯 *Confidence:* {conf_str}\n"
        f"💵 *Notional:* {notional_str}   🎯 *Target:* {target_str}   ⏳ *Horizon:* {horizon_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 *Thesis:*\n{thesis}\n"
    )

    if risks:
        msg += f"\n⚠️ *Key Risks:*\n{risks}\n"

    if analyst_lines:
        msg += f"\n🔬 *Analyst Signals:*{analyst_lines}\n"

    if block_reason:
        msg += f"\n🚫 *Blocked:* {block_reason}\n"

    msg += f"\n🔖 *Order ID:* {order_str}"

    notify(msg)


def notify_scan_start(tickers: list) -> None:
    notify(f"🔍 *Market Scan* — analyzing: `{'`, `'.join(tickers)}`")


def notify_halt(reason: str) -> None:
    notify(f"🛑 *TRADING HALTED* — {reason}")


def notify_resume() -> None:
    notify("▶️ *Trading resumed*")


def notify_error(ticker: str, error: str) -> None:
    notify(f"❌ *ERROR* on `{ticker}`: {error[:200]}")


def notify_hold(ticker: str, rating: str, confidence: Optional[float], thesis: str) -> None:
    conf_str = f"{confidence * 100:.0f}%" if confidence else "n/a"
    short_thesis = (thesis[:200] + "...") if len(thesis) > 200 else thesis
    notify(
        f"⏸️ *{ticker}* — HOLD\n"
        f"Rating: {rating}  Confidence: {conf_str}\n"
        f"_{short_thesis}_"
    )
