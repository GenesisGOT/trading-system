"""
routers/predictions.py
======================
Robinhood prediction market contracts — manual tracking.
Counted as the "predictions" sleeve in allocation math.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from deps import SessionDep, get_current_username
from models.trade_models import PredictionContract

router = APIRouter(prefix="/predictions", tags=["predictions"])


class PredictionRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=50)
    name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    entry_price: float | None = Field(default=None, ge=0)
    quantity: int | None = Field(default=None, ge=0)


def _out(c: PredictionContract) -> dict:
    return {
        "symbol": c.symbol,
        "name": c.name,
        "category": c.category,
        "entry_price": c.entry_price,
        "quantity": c.quantity,
        "notional": (c.entry_price or 0) * (c.quantity or 0),
        "active": c.active,
        "added_at": c.added_at.isoformat(),
    }


@router.get("", summary="List active prediction contracts")
def list_predictions(
    session: SessionDep,
    current_user: Annotated[str, Depends(get_current_username)],
) -> list[dict]:
    rows = session.exec(
        select(PredictionContract)
        .where(PredictionContract.active == True)
        .order_by(PredictionContract.added_at.desc())
    ).all()
    return [_out(r) for r in rows]


@router.post("", summary="Add a prediction contract")
def add_prediction(
    req: PredictionRequest,
    session: SessionDep,
    current_user: Annotated[str, Depends(get_current_username)],
) -> dict:
    symbol = req.symbol.upper().strip()
    existing = session.get(PredictionContract, symbol)
    if existing:
        existing.name = req.name or existing.name
        existing.category = req.category or existing.category
        existing.entry_price = req.entry_price if req.entry_price is not None else existing.entry_price
        existing.quantity = req.quantity if req.quantity is not None else existing.quantity
        existing.active = True
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return _out(existing)

    row = PredictionContract(
        symbol=symbol,
        name=req.name,
        category=req.category,
        entry_price=req.entry_price,
        quantity=req.quantity,
        added_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _out(row)


@router.delete("/{symbol}", summary="Remove (deactivate) a prediction contract")
def remove_prediction(
    symbol: str,
    session: SessionDep,
    current_user: Annotated[str, Depends(get_current_username)],
) -> dict:
    row = session.get(PredictionContract, symbol.upper())
    if not row:
        raise HTTPException(status_code=404, detail="Contract not found")
    row.active = False
    session.add(row)
    session.commit()
    return {"symbol": row.symbol, "active": False}
