"""SQLite audit ledger — every agent decision and trade execution is recorded here."""
from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import settings

_DB_PATH = settings.database_path


def _connect() -> sqlite3.Connection:
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_decisions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                ticker      TEXT NOT NULL,
                analysis_date TEXT NOT NULL,
                rating      TEXT NOT NULL,
                action      TEXT NOT NULL,         -- BUY | HOLD | SELL
                confidence  REAL,
                summary     TEXT,
                investment_thesis TEXT,
                price_target REAL,
                time_horizon TEXT,
                raw_state   TEXT,                  -- full JSON of final_state
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS analyst_reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL REFERENCES agent_decisions(id),
                analyst     TEXT NOT NULL,          -- fundamental | technical | sentiment | news
                report      TEXT,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trade_executions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id     INTEGER REFERENCES agent_decisions(id),
                ticker          TEXT NOT NULL,
                side            TEXT NOT NULL,      -- buy | sell
                quantity        REAL NOT NULL,
                notional_usd    REAL,
                order_id        TEXT,
                status          TEXT NOT NULL,      -- submitted | blocked | dry_run | error
                block_reason    TEXT,
                broker_response TEXT,               -- JSON
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS system_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,          -- halt | resume | startup | error
                detail      TEXT,
                created_at  TEXT NOT NULL
            );
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_decision(
    run_id: str,
    ticker: str,
    analysis_date: str,
    rating: str,
    action: str,
    confidence: Optional[float],
    summary: str,
    investment_thesis: str,
    price_target: Optional[float],
    time_horizon: Optional[str],
    raw_state: Dict[str, Any],
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO agent_decisions
               (run_id, ticker, analysis_date, rating, action, confidence,
                summary, investment_thesis, price_target, time_horizon, raw_state, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, ticker, analysis_date, rating, action, confidence,
                summary, investment_thesis, price_target, time_horizon,
                json.dumps(raw_state, default=str), _now(),
            ),
        )
        return cur.lastrowid


def log_analyst_report(decision_id: int, analyst: str, report: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO analyst_reports (decision_id, analyst, report, created_at) VALUES (?,?,?,?)",
            (decision_id, analyst, report, _now()),
        )


def log_execution(
    decision_id: Optional[int],
    ticker: str,
    side: str,
    quantity: float,
    notional_usd: Optional[float],
    order_id: Optional[str],
    status: str,
    block_reason: Optional[str],
    broker_response: Optional[Dict],
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO trade_executions
               (decision_id, ticker, side, quantity, notional_usd, order_id,
                status, block_reason, broker_response, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, ticker, side, quantity, notional_usd, order_id,
                status, block_reason,
                json.dumps(broker_response, default=str) if broker_response else None,
                _now(),
            ),
        )
        return cur.lastrowid


def log_system_event(event_type: str, detail: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO system_events (event_type, detail, created_at) VALUES (?,?,?)",
            (event_type, detail, _now()),
        )


def get_recent_decisions(limit: int = 50) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_decisions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_executions(limit: int = 50) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trade_executions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_daily_trade_count(ticker: Optional[str] = None) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    with get_db() as conn:
        if ticker:
            row = conn.execute(
                "SELECT COUNT(*) FROM trade_executions WHERE status='submitted' AND ticker=? AND created_at LIKE ?",
                (ticker, f"{today}%"),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM trade_executions WHERE status='submitted' AND created_at LIKE ?",
                (f"{today}%",),
            ).fetchone()
        return row[0]
