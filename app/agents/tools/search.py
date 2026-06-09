"""Tavily real-time search — gives agents live news, filings, and sentiment."""
from __future__ import annotations

import logging
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)


def search_ticker_news(ticker: str, query: str = "") -> str:
    """Search for latest news and analysis on a ticker. Returns a text summary."""
    if not settings.tavily_api_key:
        return ""

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=settings.tavily_api_key)
        q = query or f"{ticker} stock news analysis earnings outlook {settings.analysis_date_override or 'today'}"
        result = client.search(
            query=q,
            search_depth="advanced",
            max_results=5,
            include_answer=True,
        )
        # Build a clean summary for agents to read
        lines = []
        if result.get("answer"):
            lines.append(f"SUMMARY: {result['answer']}\n")
        for r in result.get("results", []):
            title = r.get("title", "")
            content = r.get("content", "")[:300]
            url = r.get("url", "")
            lines.append(f"• {title}\n  {content}\n  Source: {url}")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("[%s] Tavily search failed: %s", ticker, exc)
        return ""


def search_options_flow(ticker: str) -> str:
    """Search for options flow, dark pool, and unusual activity."""
    if not settings.tavily_api_key:
        return ""
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=settings.tavily_api_key)
        result = client.search(
            query=f"{ticker} unusual options activity dark pool institutional flow",
            search_depth="basic",
            max_results=3,
            include_answer=True,
        )
        lines = []
        if result.get("answer"):
            lines.append(result["answer"])
        for r in result.get("results", [])[:2]:
            lines.append(f"• {r.get('title', '')}: {r.get('content', '')[:200]}")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("[%s] Options flow search failed: %s", ticker, exc)
        return ""


def search_macro_context() -> str:
    """Pull current macro environment: Fed, rates, sector rotation."""
    if not settings.tavily_api_key:
        return ""
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=settings.tavily_api_key)
        result = client.search(
            query="stock market outlook today Fed rates inflation sector rotation",
            search_depth="basic",
            max_results=3,
            include_answer=True,
        )
        return result.get("answer", "")
    except Exception as exc:
        log.warning("Macro search failed: %s", exc)
        return ""
