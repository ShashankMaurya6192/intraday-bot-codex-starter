"""Deterministic paper-execution state with explicit simulation provenance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from trading_bot.domain import IST, Exchange
from trading_bot.risk import RiskDecision, TradeSide


class PaperOrderPurpose(StrEnum):
    ENTRY = "ENTRY"
    HARD_STOP = "HARD_STOP"
    PARTIAL_ONE_R = "PARTIAL_ONE_R"
    PARTIAL_ONE_POINT_FIVE_R = "PARTIAL_ONE_POINT_FIVE_R"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_EXIT = "TIME_EXIT"
    KILL_SWITCH_EXIT = "KILL_SWITCH_EXIT"


class PaperOrderStatus(StrEnum):
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class PaperPositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class PaperTradeIntent:
    client_tag: str
    session_date: date
    exchange: Exchange
    symbol_token: str
    trading_symbol: str
    side: TradeSide
    reference_entry_price: Decimal
    protective_stop: Decimal
    tick_size: Decimal
    lot_size: int
    max_liquidity_quantity: int | None
    submitted_at: datetime
    signal_qualified: bool
    market_data_trusted: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", Exchange(self.exchange))
        object.__setattr__(self, "side", TradeSide(self.side))
        for name in ("client_tag", "symbol_token", "trading_symbol"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must not be blank")
            object.__setattr__(self, name, value)
        if len(self.client_tag) > 64:
            raise ValueError("client_tag must not exceed 64 characters")
        for name in ("reference_entry_price", "protective_stop", "tick_size"):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        if self.lot_size < 1:
            raise ValueError("lot_size must be positive")
        if self.max_liquidity_quantity is not None and self.max_liquidity_quantity < 1:
            raise ValueError("max_liquidity_quantity must be positive when provided")
        submitted = _aware(self.submitted_at)
        if submitted.date() != self.session_date:
            raise ValueError("paper intent must belong to its IST session date")
        object.__setattr__(self, "submitted_at", submitted)


@dataclass(frozen=True, slots=True)
class PaperTradingDay:
    session_date: date
    starting_equity: Decimal
    realized_pnl: Decimal = Decimal(0)
    capital_reserved: Decimal = Decimal(0)
    trade_count: int = 0
    consecutive_losses: int = 0
    kill_switch_active: bool = False
    state_uncertain: bool = False
    updated_at: datetime = field(default_factory=lambda: datetime.now(IST))

    def __post_init__(self) -> None:
        for name in ("starting_equity", "realized_pnl", "capital_reserved"):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, value)
        if self.starting_equity <= 0:
            raise ValueError("starting_equity must be positive")
        if self.capital_reserved < 0:
            raise ValueError("capital_reserved must be non-negative")
        if self.trade_count < 0 or self.consecutive_losses < 0:
            raise ValueError("paper day counters must be non-negative")
        updated = _aware(self.updated_at)
        if updated.date() != self.session_date:
            raise ValueError("paper day timestamp must match session date")
        object.__setattr__(self, "updated_at", updated)


@dataclass(frozen=True, slots=True)
class PaperOrder:
    order_id: str
    client_tag: str
    session_date: date
    exchange: Exchange
    symbol_token: str
    trading_symbol: str
    side: TradeSide
    purpose: PaperOrderPurpose
    requested_quantity: int
    filled_quantity: int
    reference_price: Decimal
    protective_stop: Decimal | None
    planned_risk_per_share: Decimal | None
    tick_size: Decimal
    lot_size: int
    status: PaperOrderStatus
    created_at: datetime
    updated_at: datetime
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", Exchange(self.exchange))
        object.__setattr__(self, "side", TradeSide(self.side))
        object.__setattr__(self, "purpose", PaperOrderPurpose(self.purpose))
        object.__setattr__(self, "status", PaperOrderStatus(self.status))
        for name in ("order_id", "client_tag", "symbol_token", "trading_symbol"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must not be blank")
            object.__setattr__(self, name, value)
        if self.requested_quantity < 0:
            raise ValueError("requested_quantity must be non-negative")
        if not 0 <= self.filled_quantity <= self.requested_quantity:
            raise ValueError("filled_quantity is outside order bounds")
        if (
            self.requested_quantity == 0
            and self.status is not PaperOrderStatus.REJECTED
        ):
            raise ValueError("only rejected paper orders may have zero quantity")
        if self.status is PaperOrderStatus.REJECTED and not self.rejection_reason:
            raise ValueError("rejected paper orders require a reason")
        if (
            self.status is PaperOrderStatus.FILLED
            and self.filled_quantity != self.requested_quantity
        ):
            raise ValueError("filled paper order must have no remaining quantity")
        if self.status is PaperOrderStatus.PENDING and self.filled_quantity:
            raise ValueError("pending paper order cannot have filled quantity")
        if self.status is PaperOrderStatus.PARTIALLY_FILLED and not (
            0 < self.filled_quantity < self.requested_quantity
        ):
            raise ValueError("partially filled order has invalid quantity")
        if self.status is PaperOrderStatus.REJECTED and self.filled_quantity:
            raise ValueError("rejected paper order cannot have a fill")
        for name in ("reference_price", "tick_size"):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        for name in ("protective_stop", "planned_risk_per_share"):
            value = getattr(self, name)
            if value is not None:
                value = Decimal(str(value))
                if not value.is_finite() or value <= 0:
                    raise ValueError(f"{name} must be finite and positive")
                object.__setattr__(self, name, value)
        if self.lot_size < 1:
            raise ValueError("lot_size must be positive")
        created = _aware(self.created_at)
        updated = _aware(self.updated_at)
        if (
            updated < created
            or created.date() != self.session_date
            or updated.date() != self.session_date
        ):
            raise ValueError("paper order timestamps are invalid")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)

    @property
    def remaining_quantity(self) -> int:
        return self.requested_quantity - self.filled_quantity

    @property
    def active(self) -> bool:
        return self.status in {
            PaperOrderStatus.PENDING,
            PaperOrderStatus.PARTIALLY_FILLED,
        }


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: str
    order_id: str
    session_date: date
    exchange: Exchange
    symbol_token: str
    side: TradeSide
    quantity: int
    price: Decimal
    filled_at: datetime
    simulation_model: str = "NEXT_TRUSTED_TICK_ADVERSE_SLIPPAGE_V1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", Exchange(self.exchange))
        object.__setattr__(self, "side", TradeSide(self.side))
        if not self.fill_id.strip() or not self.order_id.strip():
            raise ValueError("fill/order identity must not be blank")
        if self.quantity < 1:
            raise ValueError("fill quantity must be positive")
        price = Decimal(str(self.price))
        if not price.is_finite() or price <= 0:
            raise ValueError("fill price must be finite and positive")
        object.__setattr__(self, "price", price)
        filled = _aware(self.filled_at)
        if filled.date() != self.session_date:
            raise ValueError("fill must belong to its IST session date")
        object.__setattr__(self, "filled_at", filled)


@dataclass(frozen=True, slots=True)
class PaperPosition:
    session_date: date
    exchange: Exchange
    symbol_token: str
    trading_symbol: str
    side: TradeSide
    entry_order_id: str
    initial_quantity: int
    remaining_quantity: int
    lot_size: int
    average_entry_price: Decimal
    last_mark_price: Decimal
    last_mark_at: datetime
    protective_stop: Decimal
    planned_risk_per_share: Decimal
    tick_size: Decimal
    realized_pnl: Decimal
    first_partial_started: bool
    second_partial_started: bool
    status: PaperPositionStatus
    opened_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", Exchange(self.exchange))
        object.__setattr__(self, "side", TradeSide(self.side))
        object.__setattr__(self, "status", PaperPositionStatus(self.status))
        for name in ("symbol_token", "trading_symbol", "entry_order_id"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must not be blank")
            object.__setattr__(self, name, value)
        if self.initial_quantity < 1:
            raise ValueError("initial_quantity must be positive")
        if not 0 <= self.remaining_quantity <= self.initial_quantity:
            raise ValueError("remaining_quantity is outside position bounds")
        if self.lot_size < 1:
            raise ValueError("lot_size must be positive")
        for name in (
            "average_entry_price",
            "last_mark_price",
            "protective_stop",
            "planned_risk_per_share",
            "tick_size",
        ):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        realized = Decimal(str(self.realized_pnl))
        if not realized.is_finite():
            raise ValueError("realized_pnl must be finite")
        object.__setattr__(self, "realized_pnl", realized)
        opened = _aware(self.opened_at)
        last_mark = _aware(self.last_mark_at)
        updated = _aware(self.updated_at)
        if (
            last_mark < opened
            or updated < opened
            or opened.date() != self.session_date
            or last_mark.date() != self.session_date
        ):
            raise ValueError("paper position timestamps are invalid")
        if self.status is PaperPositionStatus.CLOSED and self.remaining_quantity:
            raise ValueError("closed paper position cannot retain quantity")
        if self.status is PaperPositionStatus.OPEN and not self.remaining_quantity:
            raise ValueError("open paper position must retain quantity")
        object.__setattr__(self, "opened_at", opened)
        object.__setattr__(self, "last_mark_at", last_mark)
        object.__setattr__(self, "updated_at", updated)


@dataclass(frozen=True, slots=True)
class PaperAuditEvent:
    event_id: int
    session_date: date
    occurred_at: datetime
    event_type: str
    entity_id: str | None
    details: dict[str, object]


@dataclass(frozen=True, slots=True)
class PaperSubmissionResult:
    accepted: bool
    order: PaperOrder
    risk_decision: RiskDecision | None
    idempotent_replay: bool = False

    def __post_init__(self) -> None:
        order_accepted = self.order.status not in {
            PaperOrderStatus.REJECTED,
            PaperOrderStatus.CANCELLED,
        }
        if self.accepted != order_accepted:
            raise ValueError("submission result conflicts with paper order status")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("paper timestamp must be timezone-aware")
    return value.astimezone(IST)
