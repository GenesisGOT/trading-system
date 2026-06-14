"""
transfer_service.py
===================
Internal Robinhood account-to-account transfers (brokerage ↔ IRA).

Robinhood doesn't expose transfers in the official public API, but they
are available through the same internal REST endpoints robin_stocks uses.
All calls go through robin_stocks' authenticated session so no extra
credentials are needed beyond the standard RH login.

All transfers are logged to the transfer_log table for auditing.
Transfers are destructive — callers must pass confirmed=True.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

log = logging.getLogger(__name__)

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Robinhood internal transfer direction constants
DIRECTION_TO_IRA = "brokerage_to_ira"
DIRECTION_TO_BROKERAGE = "ira_to_brokerage"


def get_transfer_eligible_accounts() -> dict:
    """
    Returns brokerage and IRA account details if both exist.
    Result: {"brokerage": {...}, "ira": {...}, "can_transfer": bool}
    """
    from services.robinhood_service import get_all_accounts, _try_login
    if not _try_login():
        return {"brokerage": None, "ira": None, "can_transfer": False}

    accounts = get_all_accounts()
    brokerage = next((a for a in accounts if a["type"] == "individual"), None)
    ira = next((a for a in accounts if "ira" in a["type"].lower()), None)

    return {
        "brokerage": brokerage,
        "ira": ira,
        "can_transfer": bool(brokerage and ira),
    }


def initiate_transfer(
    from_account_type: str,  # "brokerage" or "ira"
    to_account_type: str,    # "brokerage" or "ira"
    amount_usd: float,
    username: str,
    confirmed: bool = False,
) -> dict:
    """
    Move cash between brokerage and IRA accounts.

    Returns dict with: status, amount_usd, direction, transfer_id, dry_run, error
    """
    if not confirmed:
        return {"status": "unconfirmed", "error": "Pass confirmed=True to execute transfer"}

    if amount_usd <= 0:
        return {"status": "error", "error": "Amount must be positive"}

    if from_account_type == to_account_type:
        return {"status": "error", "error": "Source and destination must be different accounts"}

    eligible = get_transfer_eligible_accounts()
    if not eligible["can_transfer"]:
        return {
            "status": "error",
            "error": "Could not find both brokerage and IRA accounts on this Robinhood login",
        }

    from_acct = eligible[from_account_type]
    to_acct = eligible[to_account_type]

    if not from_acct or not to_acct:
        return {"status": "error", "error": f"Account type '{from_account_type}' not found"}

    available = from_acct.get("buying_power", 0)
    if amount_usd > available:
        return {
            "status": "error",
            "error": f"Insufficient buying power: ${available:.2f} available in {from_account_type}",
        }

    if DRY_RUN:
        result = {
            "status": "dry_run",
            "amount_usd": amount_usd,
            "from": from_account_type,
            "to": to_account_type,
            "transfer_id": None,
            "dry_run": True,
        }
        _log_transfer(username, amount_usd, from_account_type, to_account_type, "dry_run", None)
        return result

    try:
        import robin_stocks.robinhood as rh

        # robin_stocks helper to POST to internal endpoints
        direction = (
            DIRECTION_TO_IRA if to_account_type == "ira" else DIRECTION_TO_BROKERAGE
        )
        payload = {
            "direction": direction,
            "amount": str(round(amount_usd, 2)),
            "from_account": from_acct["url"],
            "to_account": to_acct["url"],
        }
        response = rh.helper.request_post(
            "https://api.robinhood.com/ira/internal_transfers/",
            payload=payload,
            jsonify_data=True,
        )

        if not response or "id" not in response:
            error_msg = str(response) if response else "No response from Robinhood"
            _log_transfer(username, amount_usd, from_account_type, to_account_type, "error", None, error_msg)
            return {"status": "error", "error": error_msg}

        transfer_id = response.get("id")
        _log_transfer(username, amount_usd, from_account_type, to_account_type, "submitted", transfer_id)

        return {
            "status": "submitted",
            "amount_usd": amount_usd,
            "from": from_account_type,
            "to": to_account_type,
            "transfer_id": transfer_id,
            "dry_run": False,
        }

    except Exception as exc:
        log.error("Transfer failed: %s", exc)
        _log_transfer(username, amount_usd, from_account_type, to_account_type, "error", None, str(exc))
        return {"status": "error", "error": str(exc)}


def get_transfer_history() -> list[dict]:
    """Fetch recent internal transfers from Robinhood."""
    from services.robinhood_service import _try_login
    if not _try_login():
        return []
    try:
        import robin_stocks.robinhood as rh
        response = rh.helper.request_get(
            "https://api.robinhood.com/ira/internal_transfers/",
            jsonify_data=True,
        )
        transfers = (response or {}).get("results", [])
        return [
            {
                "id": t.get("id"),
                "direction": t.get("direction"),
                "amount": t.get("amount"),
                "state": t.get("state"),
                "created_at": t.get("created_at"),
            }
            for t in transfers
        ]
    except Exception as exc:
        log.warning("get_transfer_history failed: %s", exc)
        return []


def _log_transfer(
    username: str,
    amount_usd: float,
    from_acct: str,
    to_acct: str,
    status: str,
    transfer_id: str | None,
    error_msg: str | None = None,
) -> None:
    """Persist transfer record to DB for audit trail."""
    try:
        from models.trade_models import TransferLog
        from sqlmodel import Session
        from db.core import get_engine

        with Session(get_engine()) as session:
            row = TransferLog(
                username=username,
                from_account=from_acct,
                to_account=to_acct,
                amount_usd=amount_usd,
                transfer_id=transfer_id,
                status=status,
                error_msg=error_msg,
                created_at=datetime.now(UTC),
            )
            session.add(row)
            session.commit()
    except Exception as exc:
        log.warning("_log_transfer DB write failed: %s", exc)
