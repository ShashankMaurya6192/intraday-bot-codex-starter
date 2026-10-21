"""Broker-neutral risk decisions for paper and future execution adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_bot.domain import IST


class TradeSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class RiskRejection(StrEnum):
    SIGNAL_NOT_QUALIFIED = "signal_not_qualified"
    MARKET_DATA_UNTRUSTED = "market_data_untrusted"
    OUTSIDE_ENTRY_WINDOW = "outside_entry_window"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    STATE_UNCERTAIN = "state_uncertain"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    MAX_OPEN_POSITIONS = "max_open_positions"
    MAX_TRADES_PER_DAY = "max_trades_per_day"
    MAX_CONSECUTIVE_LOSSES = "max_consecutive_losses"
    AVAILABLE_FUNDS_UNVERIFIED = "available_funds_unverified"
    INVALID_EQUITY = "invalid_equity"
    INVALID_ENTRY_PRICE = "invalid_entry_price"
    INVALID_STOP_PRICE = "invalid_stop_price"
    INVALID_STOP_DIRECTION = "invalid_stop_direction"
    INVALID_LOT_SIZE = "invalid_lot_size"
    LIQUIDITY_UNVERIFIED = "liquidity_unverified"
    ZERO_RISK_QUANTITY = "zero_risk_quantity"
    ZERO_CAPITAL_QUANTITY = "zero_capital_quantity"
    ZERO_LIQUIDITY_QUANTITY = "zero_liquidity_quantity"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    risk_per_trade_pct: Decimal
    max_daily_loss_pct: Decimal
    max_open_positions: int
    max_trades_per_day: int
    max_consecutive_losses: int
    max_capital_utilization_pct: Decimal

    def __post_init__(self) -> None:
        for name in (
            "risk_per_trade_pct",
            "max_daily_loss_pct",
            "max_capital_utilization_pct",
        ):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value <= 0 or value > 1:
                raise ValueError(f"{name} must be finite and in (0, 1]")
            object.__setattr__(self, name, value)
        if self.risk_per_trade_pct > self.max_daily_loss_pct:
            raise ValueError("risk per trade cannot exceed daily loss limit")
        for name in (
            "max_open_positions",
            "max_trades_per_day",
            "max_consecutive_losses",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class RiskContext:
    starting_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    capital_reserved: Decimal
    available_funds: Decimal | None
    open_positions: int
    trades_today: int
    consecutive_losses: int
    kill_switch_active: bool
    state_uncertain: bool

    def __post_init__(self) -> None:
        for name in (
            "starting_equity",
            "realized_pnl",
            "unrealized_pnl",
            "capital_reserved",
        ):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, value)
        if self.available_funds is not None:
            funds = Decimal(str(self.available_funds))
            if not funds.is_finite() or funds < 0:
                raise ValueError("available_funds must be finite and non-negative")
            object.__setattr__(self, "available_funds", funds)
        if self.starting_equity <= 0:
            raise ValueError("starting_equity must be positive")
        if self.capital_reserved < 0:
            raise ValueError("capital_reserved must be non-negative")
        for name in ("open_positions", "trades_today", "consecutive_losses"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")

    @property
    def day_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def current_equity(self) -> Decimal:
        return self.starting_equity + self.day_pnl


@dataclass(frozen=True, slots=True)
class PositionSizeRequest:
    side: TradeSide
    entry_price: Decimal
    stop_price: Decimal
    lot_size: int
    max_liquidity_quantity: int | None
    evaluated_at: datetime
    signal_qualified: bool
    market_data_trusted: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", TradeSide(self.side))
        for name in ("entry_price", "stop_price"):
            value = Decimal(str(getattr(self, name)))
            object.__setattr__(self, name, value)
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        object.__setattr__(self, "evaluated_at", self.evaluated_at.astimezone(IST))


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    quantity: int
    risk_budget: Decimal
    risk_per_share: Decimal
    quantity_by_risk: int
    quantity_by_capital: int
    quantity_by_liquidity: int
    capital_required: Decimal
    planned_max_loss: Decimal
    rejection_reasons: tuple[RiskRejection, ...]

    def __post_init__(self) -> None:
        if self.approved != (self.quantity > 0 and not self.rejection_reasons):
            raise ValueError("approved risk decision must have quantity and no reasons")

    def to_dict(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "quantity": self.quantity,
            "risk_budget": str(self.risk_budget),
            "risk_per_share": str(self.risk_per_share),
            "quantity_by_risk": self.quantity_by_risk,
            "quantity_by_capital": self.quantity_by_capital,
            "quantity_by_liquidity": self.quantity_by_liquidity,
            "capital_required": str(self.capital_required),
            "planned_max_loss": str(self.planned_max_loss),
            "rejection_reasons": [item.value for item in self.rejection_reasons],
        }
