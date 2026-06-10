"""Insider trading signal — SEC EDGAR Form 4 filings via EdgarTools.

Form 4 is filed within 2 business days of any insider buy/sell.
Large insider BUYs (>$100k) are one of the strongest signals in equity markets.
Returns a formatted string for agent prompts.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

log = logging.getLogger(__name__)

MIN_TRANSACTION_VALUE = 50_000   # only flag transactions above $50k
LOOKBACK_DAYS = 30


def get_insider_activity(ticker: str) -> str:
    """Return recent insider buying/selling for a ticker as a formatted string."""
    try:
        from edgar import Company, set_identity
        set_identity("trading-bot research@example.com")

        company = Company(ticker)
        filings = company.get_filings(form="4").latest(20)

        buys, sells = [], []
        cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)

        for filing in filings:
            try:
                obj = filing.obj()
                if not obj:
                    continue
                filed_date = filing.filing_date
                if hasattr(filed_date, "date"):
                    filed_date = filed_date.date()
                if filed_date < cutoff:
                    continue

                transactions = getattr(obj, "transactions", []) or []
                for tx in transactions:
                    tx_type = getattr(tx, "transaction_code", "") or ""
                    shares   = getattr(tx, "shares", 0) or 0
                    price    = getattr(tx, "price_per_share", 0) or 0
                    value    = shares * price if shares and price else 0
                    name     = getattr(obj, "reporting_owner_name", "Insider") or "Insider"
                    title    = getattr(obj, "reporting_owner_title", "") or ""

                    if value < MIN_TRANSACTION_VALUE:
                        continue

                    entry = {
                        "name": name,
                        "title": title,
                        "shares": int(shares),
                        "price": price,
                        "value": value,
                        "date": str(filed_date),
                        "code": tx_type,
                    }

                    if tx_type in ("P",):   # Purchase
                        buys.append(entry)
                    elif tx_type in ("S", "D"):  # Sale / Disposition
                        sells.append(entry)
            except Exception:
                continue

        if not buys and not sells:
            return ""

        lines = [f"SEC INSIDER ACTIVITY ({ticker}, last {LOOKBACK_DAYS}d):"]

        if buys:
            lines.append(f"  🟢 INSIDER BUYS ({len(buys)}):")
            for b in buys[:4]:
                lines.append(
                    f"    {b['name']} ({b['title'] or 'Insider'}): "
                    f"+{b['shares']:,} shares @ ${b['price']:.2f} = ${b['value']:,.0f} on {b['date']}"
                )

        if sells:
            lines.append(f"  🔴 INSIDER SELLS ({len(sells)}):")
            for s in sells[:3]:
                lines.append(
                    f"    {s['name']}: -{s['shares']:,} shares @ ${s['price']:.2f} = ${s['value']:,.0f}"
                )

        # Signal assessment
        total_buy_value  = sum(b["value"] for b in buys)
        total_sell_value = sum(s["value"] for s in sells)
        if total_buy_value > total_sell_value * 2:
            lines.append("  📌 SIGNAL: Heavy insider buying — STRONG BULLISH indicator")
        elif total_sell_value > total_buy_value * 2:
            lines.append("  📌 SIGNAL: Heavy insider selling — BEARISH indicator")
        else:
            lines.append("  📌 SIGNAL: Mixed insider activity")

        return "\n".join(lines)

    except Exception as exc:
        log.debug("[%s] Insider activity lookup failed: %s", ticker, exc)
        return ""
