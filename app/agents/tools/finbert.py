"""FinBERT sentiment scoring for financial news headlines.

Uses ProsusAI/finBERT via HuggingFace transformers pipeline.
Falls back gracefully if transformers not installed.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

log = logging.getLogger(__name__)

_pipeline = None
_loaded = False


def _load_pipeline():
    global _pipeline, _loaded
    if _loaded:
        return _pipeline
    _loaded = True
    try:
        from transformers import pipeline
        _pipeline = pipeline(
            "text-classification",
            model="ProsusAI/finbert",
            top_k=None,
            truncation=True,
            max_length=512,
        )
        log.info("FinBERT loaded successfully")
    except Exception as exc:
        log.warning("FinBERT unavailable: %s — skipping sentiment scoring", exc)
        _pipeline = None
    return _pipeline


def score_sentiment(texts: list[str]) -> dict:
    """Score a list of headlines. Returns {positive, negative, neutral} avg scores."""
    pipe = _load_pipeline()
    if not pipe or not texts:
        return {}

    try:
        totals = {"positive": 0.0, "negative": 0.0, "neutral": 0.0}
        count = 0
        for text in texts[:10]:  # cap at 10 headlines
            if not text or len(text) < 10:
                continue
            results = pipe(text[:512])
            if results and isinstance(results[0], list):
                for r in results[0]:
                    label = r["label"].lower()
                    if label in totals:
                        totals[label] += r["score"]
            count += 1

        if count == 0:
            return {}

        avg = {k: round(v / count, 3) for k, v in totals.items()}
        dominant = max(avg, key=avg.get)
        avg["dominant"] = dominant
        avg["score"] = avg["positive"] - avg["negative"]  # -1 to +1
        return avg

    except Exception as exc:
        log.warning("FinBERT scoring failed: %s", exc)
        return {}


def get_finbert_signal(news_text: str) -> str:
    """Extract headlines from news text and return a finBERT sentiment summary."""
    if not news_text:
        return ""

    # Extract headline lines (bullet points or short lines)
    lines = [l.strip("•- ").strip() for l in news_text.split("\n") if l.strip()]
    headlines = [l for l in lines if 10 < len(l) < 200][:10]

    scores = score_sentiment(headlines)
    if not scores:
        return ""

    dominant = scores.get("dominant", "neutral")
    score = scores.get("score", 0)
    pos = scores.get("positive", 0)
    neg = scores.get("negative", 0)

    label = "BULLISH" if score > 0.15 else "BEARISH" if score < -0.15 else "NEUTRAL"
    return (
        f"[FinBERT Sentiment] {label} (score={score:+.2f}) "
        f"positive={pos:.0%} negative={neg:.0%} "
        f"dominant={dominant} across {len(headlines)} headlines"
    )
