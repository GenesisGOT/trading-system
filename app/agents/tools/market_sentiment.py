"""Market sentiment tools — Fear & Greed index, Reddit/StockTwits crowd sentiment.

All free APIs, no key required for CNN Fear & Greed.
StockTwits is public read API.
Reddit uses PRAW or direct JSON endpoint (no auth needed for public posts).
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


# ── CNN Fear & Greed ──────────────────────────────────────────────────────────

def get_fear_greed() -> str:
    """Return CNN Fear & Greed index as a formatted string for agents."""
    try:
        import httpx
        # CNN Fear & Greed public endpoint
        resp = httpx.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        score = data["fear_and_greed"]["score"]
        rating = data["fear_and_greed"]["rating"]
        prev_close = data["fear_and_greed"].get("previous_close", score)
        change = score - prev_close
        direction = "↑" if change > 0 else "↓" if change < 0 else "→"

        label = _fg_label(score)
        return (
            f"CNN Fear & Greed: {score:.0f}/100 — {rating.upper()} {label}\n"
            f"  Change from yesterday: {direction}{abs(change):.1f}\n"
            f"  Interpretation: {_fg_context(score)}"
        )
    except Exception as exc:
        log.debug("Fear & Greed fetch failed: %s", exc)
        return ""


def _fg_label(score: float) -> str:
    if score >= 75: return "🔴"
    if score >= 55: return "🟡"
    if score >= 45: return "⚪"
    if score >= 25: return "🟢"
    return "💚"


def _fg_context(score: float) -> str:
    if score >= 80:
        return "Extreme Greed — market likely overbought, consider tighter stops or reduced size"
    if score >= 60:
        return "Greed — momentum favors longs but watch for reversals"
    if score >= 40:
        return "Neutral — no strong macro bias"
    if score >= 20:
        return "Fear — potential value opportunities, market may overreact to bad news"
    return "Extreme Fear — contrarian BUY signal, but risk is elevated"


# ── StockTwits sentiment ──────────────────────────────────────────────────────

def get_stocktwits_sentiment(ticker: str) -> str:
    """Fetch crowd sentiment from StockTwits (public API, no key needed)."""
    try:
        import httpx
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        resp = httpx.get(url, timeout=10)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        messages = data.get("messages", [])
        if not messages:
            return ""

        bullish = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
        bearish = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish")
        total = bullish + bearish

        if total == 0:
            return ""

        bull_pct = bullish / total * 100
        bear_pct = bearish / total * 100
        bias = "BULLISH" if bull_pct > 60 else "BEARISH" if bear_pct > 60 else "MIXED"

        # Grab a few recent message excerpts
        snippets = [m["body"][:80] for m in messages[:3] if m.get("body")]
        excerpt = " | ".join(snippets)

        return (
            f"StockTwits ({ticker}): {bullish}🐂 bullish / {bearish}🐻 bearish "
            f"({bull_pct:.0f}% / {bear_pct:.0f}%) — {bias}\n"
            f"  Recent: {excerpt}"
        )
    except Exception as exc:
        log.debug("[%s] StockTwits failed: %s", ticker, exc)
        return ""


# ── Reddit sentiment ──────────────────────────────────────────────────────────

def get_reddit_sentiment(ticker: str) -> str:
    """Scan r/wallstreetbets, r/stocks, r/investing for ticker mentions (no auth)."""
    try:
        import httpx
        mentions = []
        subreddits = ["wallstreetbets", "stocks", "investing"]
        for sub in subreddits:
            url = f"https://www.reddit.com/r/{sub}/search.json?q={ticker}&sort=new&limit=5&t=day"
            resp = httpx.get(url, timeout=8, headers={"User-Agent": "trading-bot/1.0"})
            if resp.status_code != 200:
                continue
            posts = resp.json().get("data", {}).get("children", [])
            for p in posts:
                d = p.get("data", {})
                title = d.get("title", "")
                score = d.get("score", 0)
                if ticker.upper() in title.upper():
                    mentions.append((score, title[:100], sub))

        if not mentions:
            return ""

        mentions.sort(reverse=True)
        total = len(mentions)
        top = mentions[:3]
        lines = [f"  r/{sub} ({score}↑): {title}" for score, title, sub in top]

        return (
            f"Reddit ({ticker}): {total} mentions in last 24h across WSB/stocks/investing\n"
            + "\n".join(lines)
        )
    except Exception as exc:
        log.debug("[%s] Reddit sentiment failed: %s", ticker, exc)
        return ""


# ── Combined macro sentiment ──────────────────────────────────────────────────

def get_macro_sentiment() -> str:
    """Return combined macro sentiment context for agent enrichment."""
    fg = get_fear_greed()
    return fg if fg else "Market sentiment data unavailable."


def get_ticker_crowd_sentiment(ticker: str, asset_type: str = "stock") -> str:
    """Combined StockTwits + Reddit sentiment for a specific ticker."""
    parts = []
    if asset_type in ("stock", "option"):
        st = get_stocktwits_sentiment(ticker)
        if st:
            parts.append(st)
        rd = get_reddit_sentiment(ticker)
        if rd:
            parts.append(rd)
    elif asset_type == "crypto":
        # For crypto use ticker without USD suffix for social search
        clean = ticker.replace("-USD", "").replace("USD", "")
        rd = get_reddit_sentiment(clean)
        if rd:
            parts.append(rd)
    return "\n\n".join(parts)
