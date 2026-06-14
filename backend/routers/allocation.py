"""
routers/allocation.py
=====================
Sleeve allocation + rebalance queue endpoints.
"""

from datetime import UTC, datetime
from typing import Annotated

import apprise
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, desc, select

from deps import SessionDep, get_current_username
from models.models import User
from models.trade_models import RebalanceActionRow, SleeveAllocation
from services.allocation_service import (
    recommend_rebalance,
    should_rebalance,
    snapshot,
)

router = APIRouter(prefix="/allocation", tags=["allocation"])


def _sleeve_dict(state) -> dict:
    return {
        "total_equity": state.total_equity,
        "free_cash": state.free_cash,
        "needs_rebalance": state.needs_rebalance,
        "source": state.source,
        "sleeves": {
            name: {
                "value": s.value,
                "current_pct": s.current_pct,
                "target_pct": s.target_pct,
                "target_value": s.target_value,
                "drift": s.drift,
                "recommended_delta": s.recommended_delta,
            }
            for name, s in state.sleeves.items()
        },
    }


@router.get("", summary="Current sleeve allocation breakdown")
def current_allocation(
    current_user: Annotated[str, Depends(get_current_username)],
) -> dict:
    state = snapshot()
    return _sleeve_dict(state)


@router.get("/history", summary="Last N allocation snapshots")
def allocation_history(
    session: SessionDep,
    current_user: Annotated[str, Depends(get_current_username)],
    limit: int = 48,
) -> list[dict]:
    rows = session.exec(
        select(SleeveAllocation)
        .order_by(desc(SleeveAllocation.snapshot_at))
        .limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "total_equity": r.total_equity,
            "stocks_value": r.stocks_value,
            "crypto_value": r.crypto_value,
            "predictions_value": r.predictions_value,
            "stocks_pct": r.stocks_pct,
            "crypto_pct": r.crypto_pct,
            "predictions_pct": r.predictions_pct,
            "free_cash": r.free_cash,
            "snapshot_at": r.snapshot_at.isoformat(),
        }
        for r in rows
    ]


rebalance_router = APIRouter(prefix="/rebalance", tags=["rebalance"])


@rebalance_router.get("/pending", summary="Pending rebalance actions")
def pending_actions(
    session: SessionDep,
    current_user: Annotated[str, Depends(get_current_username)],
) -> list[dict]:
    rows = session.exec(
        select(RebalanceActionRow)
        .where(RebalanceActionRow.status == "pending")
        .order_by(desc(RebalanceActionRow.created_at))
    ).all()
    return [
        {
            "id": r.id,
            "from_sleeve": r.from_sleeve,
            "to_sleeve": r.to_sleeve,
            "amount_usd": r.amount_usd,
            "status": r.status,
            "reason": r.reason,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


def _action_row(action_id: int, session: Session) -> RebalanceActionRow:
    row = session.get(RebalanceActionRow, action_id)
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail=f"Action already {row.status}")
    return row


def _notify_user(session: Session, username: str, title: str, body: str) -> None:
    user = session.get(User, username)
    if user and user.apprise_url:
        try:
            ap = apprise.Apprise()
            for url in user.apprise_url.split(","):
                if url.strip():
                    ap.add(url.strip())
            ap.notify(title=title, body=body)
        except Exception:
            pass


@rebalance_router.post("/{action_id}/approve", summary="Approve a rebalance action")
def approve_action(
    action_id: int,
    session: SessionDep,
    current_user: Annotated[str, Depends(get_current_username)],
) -> dict:
    row = _action_row(action_id, session)
    row.status = "approved"
    row.actioned_at = datetime.now(UTC)
    session.add(row)
    session.commit()

    _notify_user(
        session, current_user,
        title="Rebalance Approved",
        body=f"Move ${row.amount_usd:.0f} from {row.from_sleeve} → {row.to_sleeve}\n{row.reason or ''}",
    )
    return {"id": row.id, "status": "approved"}


@rebalance_router.post("/{action_id}/dismiss", summary="Dismiss a rebalance action")
def dismiss_action(
    action_id: int,
    session: SessionDep,
    current_user: Annotated[str, Depends(get_current_username)],
) -> dict:
    row = _action_row(action_id, session)
    row.status = "dismissed"
    row.actioned_at = datetime.now(UTC)
    session.add(row)
    session.commit()
    return {"id": row.id, "status": "dismissed"}
