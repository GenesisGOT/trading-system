"""
robinhood_service.py
====================
Robinhood account bridge via robin_stocks.

Session persistence: on first login with store_session=True, robin_stocks
saves a token pickle to ~/.tokens/ — we redirect HOME to /app/storage so the
pickle lands on the Railway volume and survives redeploys. After that, the
service reconnects on startup without needing credentials or MFA again.

Env vars (set on Railway, never in code):
    RH_USERNAME         your Robinhood email
    RH_PASSWORD         your Robinhood password
    RH_MFA_CODE         TOTP code — only needed on FIRST login if 2FA is on
    RH_SESSION_HOME     override for session storage dir (default /app/storage)
"""

from __future__ import annotations

import logging
import os
import pathlib

log = logging.getLogger(__name__)

_logged_in: bool = False

# Robin_stocks stores sessions under ~/.tokens/ — redirect HOME to the volume
# so sessions persist across Railway redeploys.
_SESSION_HOME = os.getenv("RH_SESSION_HOME", "/app/storage")


def _redirect_home() -> None:
    """Point HOME at the volume dir so robin_stocks session pickle persists."""
    pathlib.Path(_SESSION_HOME).mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = _SESSION_HOME


def login_with_credentials(username: str, password: str, mfa_code: str | None = None) -> bool:
    """
    Explicit login — used by the /robinhood/connect endpoint for first-time
    setup (especially when MFA is required). Stores session to volume.
    """
    global _logged_in
    _redirect_home()
    try:
        import robin_stocks.robinhood as rh
        rh.login(username, password, mfa_code=mfa_code, store_session=True)
        _logged_in = True
        log.info("robin_stocks: logged in as %s (session stored)", username)
        return True
    except Exception as exc:
        log.warning("robin_stocks explicit login failed: %s", exc)
        _logged_in = False
        return False


def _try_login() -> bool:
    """
    Auto-login on service startup. Tries stored session first, then falls
    back to env-var credentials. Silent on missing creds (returns False).
    """
    global _logged_in
    if _logged_in:
        return True

    _redirect_home()

    username = os.getenv("RH_USERNAME", "").strip()
    password = os.getenv("RH_PASSWORD", "").strip()
    mfa_code = os.getenv("RH_MFA_CODE", "").strip() or None

    if not username or not password:
        return False

    try:
        import robin_stocks.robinhood as rh
        # store_session=True — loads existing pickle if it exists, saves new one if not
        rh.login(username, password, mfa_code=mfa_code, store_session=True)
        _logged_in = True
        log.info("robin_stocks: connected as %s", username)
        return True
    except Exception as exc:
        log.warning("robin_stocks login failed: %s", exc)
        return False


def _zero_account() -> dict:
    return {
        "total_equity": 0.0,
        "cash": 0.0,
        "buying_power": 0.0,
        "day_pnl": 0.0,
        "day_pnl_pct": 0.0,
        "source": "unavailable",
    }


def get_account_summary() -> dict:
    """Equity, cash, buying_power, day P&L from Robinhood brokerage account."""
    if not _try_login():
        return _zero_account()
    try:
        import robin_stocks.robinhood as rh
        profile = rh.account.build_user_profile()
        equity = float(profile.get("equity") or 0)
        cash = float(profile.get("cash") or 0)
        prev = float(profile.get("equity_previous_close") or equity)
        day_pnl = equity - prev
        return {
            "total_equity": equity,
            "cash": cash,
            "buying_power": float(profile.get("buying_power") or cash),
            "day_pnl": day_pnl,
            "day_pnl_pct": (day_pnl / prev) if prev else 0.0,
            "source": "robin_stocks",
        }
    except Exception as exc:
        log.warning("robin_stocks account summary failed: %s", exc)
        return _zero_account()


def get_all_accounts() -> list[dict]:
    """
    Returns all Robinhood accounts (brokerage + IRA if present).
    Each entry has: account_number, type, buying_power, cash, equity.
    """
    if not _try_login():
        return []
    try:
        import robin_stocks.robinhood as rh
        accounts = rh.account.get_all_accounts() or []
        result = []
        for acct in accounts:
            result.append({
                "account_number": acct.get("account_number", ""),
                "type": acct.get("type", "unknown"),
                "buying_power": float(acct.get("buying_power") or 0),
                "cash": float(acct.get("cash") or 0),
                "equity": float(acct.get("equity") or 0),
                "url": acct.get("url", ""),
            })
        return result
    except Exception as exc:
        log.warning("get_all_accounts failed: %s", exc)
        return []


def get_rh_positions() -> list[dict]:
    """Stock + option holdings from Robinhood via build_holdings()."""
    if not _try_login():
        return []
    try:
        import robin_stocks.robinhood as rh
        holdings = rh.account.build_holdings() or {}
        result = []
        for sym, data in holdings.items():
            result.append({
                "ticker": sym.upper(),
                "asset_type": "stock",
                "quantity": float(data.get("quantity") or 0),
                "average_buy_price": float(data.get("average_buy_price") or 0),
                "current_price": float(data.get("price") or 0),
                "market_value": float(data.get("equity") or 0),
                "pnl_pct": float(data.get("percent_change") or 0) / 100,
                "equity_change": float(data.get("equity_change") or 0),
                "pe_ratio": data.get("pe_ratio"),
                "type": data.get("type", "stock"),
            })
        return result
    except Exception as exc:
        log.warning("robin_stocks positions failed: %s", exc)
        return []


def get_rh_crypto_positions() -> list[dict]:
    """Crypto holdings from Robinhood."""
    if not _try_login():
        return []
    try:
        import robin_stocks.robinhood as rh
        positions = rh.crypto.get_crypto_positions() or []
        result = []
        for pos in positions:
            code = (pos.get("currency", {}) or {}).get("code", "")
            qty = float(pos.get("quantity") or 0)
            cost_bases = pos.get("cost_bases") or []
            avg_price = (
                float(cost_bases[0].get("direct_cost_basis", 0)) / qty
                if (cost_bases and qty) else 0
            )
            result.append({
                "ticker": f"{code}-USD",
                "asset_type": "crypto",
                "quantity": qty,
                "average_buy_price": avg_price,
                "current_price": 0.0,  # enriched by caller via yfinance
                "market_value": 0.0,
                "pnl_pct": 0.0,
                "type": "crypto",
            })
        return [p for p in result if p["quantity"] > 0]
    except Exception as exc:
        log.warning("robin_stocks crypto positions failed: %s", exc)
        return []


def is_connected() -> bool:
    return _try_login()
