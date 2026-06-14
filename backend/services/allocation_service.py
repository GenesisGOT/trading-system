"""
allocation_service.py
=====================
Sleeve allocation math — stocks 75% / crypto 15% / predictions 10%.

Public API:
  snapshot()                  → AllocationState (reads live RH data)
  compute_allocation(...)     → AllocationState (pure math, no I/O)
  should_rebalance(state)     → bool
  recommend_rebalance(state)  → list[RebalanceAction]
  save_snapshot(state)        → None (persists to sleeve_allocations)
  create_rebalance_actions()  → list[int] (persists pending rows)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import List

log = logging.getLogger(__name__)

TARGETS: dict[str, float] = {
    "stocks": 0.75,
    "crypto": 0.15,
    "predictions": 0.10,
}

DRIFT_THRESHOLD = 0.05  # 5% absolute drift triggers alert
MIN_MOVE_USD = 10.0     # ignore moves smaller than $10


@dataclass
class SleeveState:
    name: str
    value: float
    target_pct: float
    current_pct: float
    target_value: float
    drift: float              # current_pct - target_pct
    recommended_delta: float  # positive = add money, negative = remove


@dataclass
class AllocationState:
    total_equity: float
    free_cash: float
    sleeves: dict[str, SleeveState] = field(default_factory=dict)
    needs_rebalance: bool = False
    source: str = "unknown"


@dataclass
class RebalanceAction:
    from_sleeve: str
    to_sleeve: str
    amount_usd: float
    reason: str


def compute_allocation(
    total_equity: float,
    sleeve_values: dict[str, float],
    free_cash: float = 0.0,
) -> AllocationState:
    """Pure math — no I/O."""
    invested = sum(sleeve_values.values())
    base = max(invested, 1.0)

    sleeves: dict[str, SleeveState] = {}
    for name, target_pct in TARGETS.items():
        value = float(sleeve_values.get(name) or 0.0)
        current_pct = value / base
        target_value = target_pct * base
        drift = current_pct - target_pct
        sleeves[name] = SleeveState(
            name=name,
            value=value,
            target_pct=target_pct,
            current_pct=current_pct,
            target_value=target_value,
            drift=drift,
            recommended_delta=target_value - value,
        )

    needs_rebalance = any(abs(s.drift) > DRIFT_THRESHOLD for s in sleeves.values())
    return AllocationState(
        total_equity=total_equity,
        free_cash=free_cash,
        sleeves=sleeves,
        needs_rebalance=needs_rebalance,
    )


def should_rebalance(state: AllocationState) -> bool:
    return state.needs_rebalance


def recommend_rebalance(state: AllocationState) -> List[RebalanceAction]:
    actions: List[RebalanceAction] = []

    over = sorted(
        [s for s in state.sleeves.values() if s.drift > 0],
        key=lambda s: s.drift, reverse=True,
    )
    under = sorted(
        [s for s in state.sleeves.values() if s.drift < 0],
        key=lambda s: s.drift,
    )

    # Deploy free cash to under-allocated sleeves first
    deployable = state.free_cash
    for u in under:
        if deployable <= MIN_MOVE_USD:
            break
        needed = abs(u.recommended_delta)
        move = min(needed, deployable)
        if move >= MIN_MOVE_USD:
            actions.append(RebalanceAction(
                from_sleeve="cash",
                to_sleeve=u.name,
                amount_usd=round(move, 2),
                reason=(
                    f"{u.name} is {abs(u.drift):.0%} under target "
                    f"({u.current_pct:.0%} vs {u.target_pct:.0%}). Deploy free cash."
                ),
            ))
            deployable -= move

    # Then suggest cross-sleeve moves
    for o in over:
        excess = o.value - o.target_value
        if excess < MIN_MOVE_USD:
            continue
        for u in under:
            shortfall = abs(u.recommended_delta)
            if shortfall < MIN_MOVE_USD:
                continue
            move = min(excess, shortfall)
            if move < MIN_MOVE_USD:
                continue
            actions.append(RebalanceAction(
                from_sleeve=o.name,
                to_sleeve=u.name,
                amount_usd=round(move, 2),
                reason=(
                    f"{o.name} over by {o.drift:.0%} ({o.current_pct:.0%} vs {o.target_pct:.0%}). "
                    f"{u.name} under by {abs(u.drift):.0%}."
                ),
            ))
            excess -= move
            u.recommended_delta += move
            if excess < MIN_MOVE_USD:
                break

    return actions


def snapshot() -> AllocationState:
    """Read live Robinhood data and compute current allocation."""
    from services.robinhood_service import get_account_summary, get_rh_positions, get_rh_crypto_positions
    from models.trade_models import PredictionContract
    from sqlmodel import Session, select
    from db.core import get_engine

    try:
        acct = get_account_summary()
        rh_positions = get_rh_positions()
        crypto_positions = get_rh_crypto_positions()

        stocks_value = sum(p["market_value"] for p in rh_positions if p["asset_type"] == "stock")

        crypto_value = 0.0
        for pos in crypto_positions:
            try:
                import yfinance as yf
                price = float(yf.Ticker(pos["ticker"]).fast_info.get("lastPrice") or 0)
                crypto_value += price * pos["quantity"]
            except Exception:
                crypto_value += pos.get("market_value", 0)

        predictions_value = 0.0
        with Session(get_engine()) as session:
            contracts = session.exec(
                select(PredictionContract).where(PredictionContract.active == True)
            ).all()
            for c in contracts:
                predictions_value += (c.entry_price or 0) * (c.quantity or 0)

        state = compute_allocation(
            total_equity=acct["total_equity"],
            sleeve_values={
                "stocks": stocks_value,
                "crypto": crypto_value,
                "predictions": predictions_value,
            },
            free_cash=acct["cash"],
        )
        state.source = acct["source"]
        return state

    except Exception as exc:
        log.warning("Allocation snapshot failed: %s", exc)
        return AllocationState(total_equity=0, free_cash=0, source="error")


def save_snapshot(state: AllocationState) -> None:
    from models.trade_models import SleeveAllocation
    from sqlmodel import Session
    from db.core import get_engine

    try:
        with Session(get_engine()) as session:
            row = SleeveAllocation(
                total_equity=state.total_equity,
                stocks_value=state.sleeves.get("stocks", SleeveState("stocks",0,0,0,0,0,0)).value,
                crypto_value=state.sleeves.get("crypto", SleeveState("crypto",0,0,0,0,0,0)).value,
                predictions_value=state.sleeves.get("predictions", SleeveState("predictions",0,0,0,0,0,0)).value,
                stocks_pct=state.sleeves.get("stocks", SleeveState("stocks",0,0,0,0,0,0)).current_pct,
                crypto_pct=state.sleeves.get("crypto", SleeveState("crypto",0,0,0,0,0,0)).current_pct,
                predictions_pct=state.sleeves.get("predictions", SleeveState("predictions",0,0,0,0,0,0)).current_pct,
                free_cash=state.free_cash,
                snapshot_at=datetime.now(UTC),
            )
            session.add(row)
            session.commit()
    except Exception as exc:
        log.warning("save_snapshot failed: %s", exc)


def create_rebalance_actions(actions: List[RebalanceAction]) -> List[int]:
    from models.trade_models import RebalanceActionRow
    from sqlmodel import Session
    from db.core import get_engine

    ids: List[int] = []
    try:
        with Session(get_engine()) as session:
            for a in actions:
                row = RebalanceActionRow(
                    from_sleeve=a.from_sleeve,
                    to_sleeve=a.to_sleeve,
                    amount_usd=a.amount_usd,
                    reason=a.reason,
                    created_at=datetime.now(UTC),
                )
                session.add(row)
                session.flush()
                ids.append(row.id)
            session.commit()
    except Exception as exc:
        log.warning("create_rebalance_actions failed: %s", exc)
    return ids
