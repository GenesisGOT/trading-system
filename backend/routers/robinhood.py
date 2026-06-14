"""
routers/robinhood.py
====================
Robinhood account read + connect + transfer endpoints.
All routes require a valid JWT.
"""

from typing import Annotated

import yfinance as yf
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from deps import SessionDep, get_current_username
from services.robinhood_service import (
    get_account_summary,
    get_all_accounts,
    get_rh_crypto_positions,
    get_rh_positions,
    is_connected,
    login_with_credentials,
)
from services.transfer_service import (
    get_transfer_eligible_accounts,
    get_transfer_history,
    initiate_transfer,
)

router = APIRouter(prefix="/robinhood", tags=["robinhood"])


# ── Connect ────────────────────────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    mfa_code: str | None = Field(default=None)


@router.post("/connect", summary="First-time login — required when MFA is enabled")
def connect(
    req: ConnectRequest,
    current_user: Annotated[str, Depends(get_current_username)],
) -> dict:
    """
    Authenticates with Robinhood and stores the session to the volume.
    After this, the service reconnects automatically on restart — you
    only need to call this once (or after a session expires).
    """
    ok = login_with_credentials(req.username, req.password, req.mfa_code or None)
    if not ok:
        raise HTTPException(status_code=401, detail="Robinhood login failed — check credentials or MFA code")
    return {"connected": True, "message": "Session stored — future restarts will reconnect automatically"}


# ── Account ────────────────────────────────────────────────────────────────────

@router.get("/status", summary="Connection status")
def status(current_user: Annotated[str, Depends(get_current_username)]) -> dict:
    connected = is_connected()
    return {
        "connected": connected,
        "message": (
            "Robinhood connected via robin_stocks"
            if connected
            else "Not connected — call POST /api/robinhood/connect or set RH_USERNAME/RH_PASSWORD"
        ),
    }


@router.get("/account", summary="Robinhood account summary")
def account(current_user: Annotated[str, Depends(get_current_username)]) -> dict:
    """Total equity, cash, buying power, and day P&L."""
    return get_account_summary()


@router.get("/accounts", summary="All Robinhood accounts (brokerage + IRA)")
def accounts(current_user: Annotated[str, Depends(get_current_username)]) -> list[dict]:
    """Returns each account type with its buying power and equity."""
    return get_all_accounts()


@router.get("/positions", summary="All Robinhood positions (stocks + crypto)")
def positions(current_user: Annotated[str, Depends(get_current_username)]) -> list[dict]:
    """
    Unified position list across stocks and crypto.
    Crypto positions are enriched with live prices from yfinance.
    """
    stock_pos = get_rh_positions()
    crypto_pos = get_rh_crypto_positions()

    for pos in crypto_pos:
        try:
            ticker = yf.Ticker(pos["ticker"])
            price = ticker.fast_info.get("lastPrice") or 0
            pos["current_price"] = float(price)
            pos["market_value"] = float(price) * pos["quantity"]
            entry = pos["average_buy_price"]
            pos["pnl_pct"] = ((price - entry) / entry) if entry else 0.0
        except Exception:
            pass

    return stock_pos + crypto_pos


# ── Transfers ──────────────────────────────────────────────────────────────────

@router.get("/transfer/accounts", summary="Accounts eligible for internal transfer")
def transfer_accounts(current_user: Annotated[str, Depends(get_current_username)]) -> dict:
    """Returns brokerage and IRA account details and whether a transfer is possible."""
    return get_transfer_eligible_accounts()


@router.get("/transfer/history", summary="Recent brokerage ↔ IRA transfer history")
def transfer_history(current_user: Annotated[str, Depends(get_current_username)]) -> list[dict]:
    return get_transfer_history()


class TransferRequest(BaseModel):
    from_account: str = Field(description="'brokerage' or 'ira'")
    to_account: str = Field(description="'brokerage' or 'ira'")
    amount_usd: float = Field(gt=0)
    confirmed: bool = Field(default=False, description="Must be true to execute")


@router.post("/transfer", summary="Move cash between brokerage and IRA")
def transfer(
    req: TransferRequest,
    current_user: Annotated[str, Depends(get_current_username)],
) -> dict:
    """
    Initiates an internal Robinhood transfer between brokerage and IRA.
    Set DRY_RUN=true on Railway to test without moving real money.
    confirmed=true is required to execute — acts as a safety gate.
    """
    valid_types = {"brokerage", "ira"}
    if req.from_account not in valid_types or req.to_account not in valid_types:
        raise HTTPException(status_code=400, detail="from_account and to_account must be 'brokerage' or 'ira'")

    result = initiate_transfer(
        from_account_type=req.from_account,
        to_account_type=req.to_account,
        amount_usd=req.amount_usd,
        username=current_user,
        confirmed=req.confirmed,
    )

    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["error"])

    return result
