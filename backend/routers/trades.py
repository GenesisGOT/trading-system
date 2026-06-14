"""
routers/trades.py
=================
Trade execution endpoints. The user confirms in the dashboard UI before
any order is sent — this router never auto-trades.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, desc, select

from deps import SessionDep, get_current_username
from models.trade_models import TradeExecution
from services.trade_service import place_order

router = APIRouter(prefix="/trades", tags=["trades"])


class PlaceOrderRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=20)
    side: str = Field(pattern="^(buy|sell)$")
    quantity: float = Field(gt=0)
    order_type: str = Field(default="market", pattern="^(market|limit)$")
    asset_type: str = Field(default="stock", pattern="^(stock|crypto|prediction)$")
    limit_price: float | None = Field(default=None, gt=0)


@router.post("/place", summary="Place a trade order via Robinhood")
def place(
    req: PlaceOrderRequest,
    current_user: Annotated[str, Depends(get_current_username)],
) -> dict:
    """
    Submit an order to Robinhood via RobinhoodMCP.
    Set DRY_RUN=true in config.env to log without sending.
    """
    return place_order(
        ticker=req.ticker,
        side=req.side,
        quantity=req.quantity,
        order_type=req.order_type,
        asset_type=req.asset_type,
        limit_price=req.limit_price,
        username=current_user,
    )


@router.get("/history", summary="Recent trade execution history")
def history(
    session: SessionDep,
    current_user: Annotated[str, Depends(get_current_username)],
    limit: int = 50,
) -> list[dict]:
    rows = session.exec(
        select(TradeExecution)
        .where(TradeExecution.username == current_user)
        .order_by(desc(TradeExecution.created_at))
        .limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "ticker": r.ticker,
            "side": r.side,
            "quantity": r.quantity,
            "order_type": r.order_type,
            "asset_type": r.asset_type,
            "limit_price": r.limit_price,
            "order_id": r.order_id,
            "status": r.status,
            "error_msg": r.error_msg,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
