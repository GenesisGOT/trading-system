"""
trade_models.py
===============
SQLModel table definitions for trade execution, allocation tracking, and predictions.
These are appended to the existing stonks schema via Alembic migrations.
"""

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class TradeExecution(SQLModel, table=True):
    __tablename__ = "trade_execution"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    ticker: str = Field(max_length=20)
    side: str = Field(max_length=4)          # buy | sell
    quantity: float
    order_type: str = Field(max_length=10)   # market | limit
    asset_type: str = Field(max_length=15)   # stock | crypto | prediction
    limit_price: float | None = Field(default=None)
    order_id: str | None = Field(default=None, max_length=100)
    status: str = Field(max_length=20)       # submitted | dry_run | error
    error_msg: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SleeveAllocation(SQLModel, table=True):
    __tablename__ = "sleeve_allocation"

    id: int | None = Field(default=None, primary_key=True)
    total_equity: float = Field(default=0.0)
    stocks_value: float = Field(default=0.0)
    crypto_value: float = Field(default=0.0)
    predictions_value: float = Field(default=0.0)
    stocks_pct: float = Field(default=0.0)
    crypto_pct: float = Field(default=0.0)
    predictions_pct: float = Field(default=0.0)
    free_cash: float = Field(default=0.0)
    snapshot_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RebalanceActionRow(SQLModel, table=True):
    __tablename__ = "rebalance_action"

    id: int | None = Field(default=None, primary_key=True)
    from_sleeve: str = Field(max_length=20)
    to_sleeve: str = Field(max_length=20)
    amount_usd: float
    status: str = Field(default="pending", max_length=20)  # pending | approved | dismissed
    reason: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actioned_at: datetime | None = Field(default=None)


class TransferLog(SQLModel, table=True):
    __tablename__ = "transfer_log"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    from_account: str = Field(max_length=20)   # brokerage | ira
    to_account: str = Field(max_length=20)     # brokerage | ira
    amount_usd: float
    transfer_id: str | None = Field(default=None, max_length=100)
    status: str = Field(max_length=20)         # submitted | dry_run | error
    error_msg: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PredictionContract(SQLModel, table=True):
    __tablename__ = "prediction_contract"

    symbol: str = Field(primary_key=True, max_length=50)
    name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    entry_price: float | None = Field(default=None, ge=0)
    quantity: int | None = Field(default=None, ge=0)
    active: bool = Field(default=True)
    added_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
