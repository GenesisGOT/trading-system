"""Validator agent — Devil's Advocate stress-test before execution.

After the Fund Manager decides BUY or SELL, this agent:
  1. Reads all analyst reports + the Fund Manager thesis
  2. Actively tries to find reasons the trade is WRONG
  3. Returns a confidence score (0-1) and a risk summary
  4. Trade is blocked if confidence < CONFIDENCE_THRESHOLD

This is the "deep research validation" layer.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.35  # block trade if validator confidence is below this


@dataclass
class ValidationResult:
    confidence: float          # 0.0 = very uncertain, 1.0 = highly confident
    proceed: bool              # True = execute, False = skip
    risk_summary: str          # key risks / concerns
    validation_notes: str      # full validator reasoning


def _build_prompt(
    ticker: str,
    action: str,
    rating: str,
    investment_thesis: str,
    analyst_reports: dict,
) -> str:
    reports_text = "\n\n".join(
        f"=== {name.upper()} ANALYST ===\n{report}"
        for name, report in analyst_reports.items()
        if report
    )
    return f"""You are a Devil's Advocate risk analyst. Your job is to STRESS-TEST the following trading decision and find every reason it could be WRONG.

PROPOSED TRADE:
  Ticker: {ticker}
  Action: {action}
  Agent Rating: {rating}
  Investment Thesis: {investment_thesis}

ANALYST REPORTS:
{reports_text}

Your task:
1. Identify the top 3 risks or flaws in this thesis
2. Look for contradictions between analyst reports
3. Consider macro/sector risks not mentioned
4. Assign a confidence score from 0.0 to 1.0:
   - 0.0-0.3: High risk, thesis has major flaws, do NOT trade
   - 0.3-0.6: Moderate confidence, some concerns but tradeable
   - 0.6-1.0: High confidence, thesis is solid

Respond in this exact format:
CONFIDENCE: [0.0-1.0]
RISKS: [2-3 sentence summary of top risks]
NOTES: [Full reasoning, max 200 words]
"""


def _parse_response(text: str) -> ValidationResult:
    confidence = 0.5
    risks = ""
    notes = text

    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("CONFIDENCE:"):
            try:
                confidence = float(line.split(":", 1)[1].strip())
                confidence = max(0.0, min(1.0, confidence))
            except ValueError:
                pass
        elif line.startswith("RISKS:"):
            risks = line.split(":", 1)[1].strip()
        elif line.startswith("NOTES:"):
            notes = line.split(":", 1)[1].strip()

    return ValidationResult(
        confidence=confidence,
        proceed=confidence >= CONFIDENCE_THRESHOLD,
        risk_summary=risks,
        validation_notes=notes,
    )


def validate_decision(
    ticker: str,
    action: str,
    rating: str,
    investment_thesis: str,
    analyst_reports: dict,
) -> ValidationResult:
    """Synchronous validator — run in asyncio.to_thread from the scheduler."""
    if action == "HOLD":
        return ValidationResult(
            confidence=1.0, proceed=True,
            risk_summary="No trade — nothing to validate.",
            validation_notes="HOLD decision requires no execution.",
        )

    try:
        from langchain_groq import ChatGroq
        llm = ChatGroq(
            model=settings.quick_think_llm,
            api_key=settings.groq_api_key,
            temperature=0.3,
        )
    except ImportError:
        try:
            from langchain_openai import ChatOpenAI
            llm = ChatOpenAI(model=settings.quick_think_llm, temperature=0.3)
        except Exception:
            log.warning("No LLM available for validator — defaulting to proceed")
            return ValidationResult(
                confidence=0.6, proceed=True,
                risk_summary="Validator skipped — LLM unavailable.",
                validation_notes="",
            )

    prompt = _build_prompt(ticker, action, rating, investment_thesis, analyst_reports)

    try:
        response = llm.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        result = _parse_response(text)
        log.info("[%s] Validator: confidence=%.2f proceed=%s", ticker, result.confidence, result.proceed)
        return result
    except Exception as exc:
        log.error("[%s] Validator failed: %s", ticker, exc)
        return ValidationResult(
            confidence=0.5, proceed=True,
            risk_summary=f"Validator error: {exc}",
            validation_notes="",
        )
