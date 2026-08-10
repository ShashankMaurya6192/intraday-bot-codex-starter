"""Broker-neutral market-data health and candle models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import IntEnum, StrEnum

from trading_bot.domain import IST, Exchange


class MarketDataHealthStatus(StrEnum):
    NOT_READY = "NOT_READY"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"


class CandleInterval(IntEnum):
    ONE_MINUTE = 1
    FIVE_MINUTES = 5

    @property
    def label(self) -> str:
        return f"{int(self)}m"


@dataclass(frozen=True, slots=True)
class InstrumentHealth:
    exchange: Exchange
    symbol_token: str
    feed_epoch: int
    status: MarketDataHealthStatus
    latest_exchange_timestamp: datetime | None
    latest_received_timestamp: datetime | None
    sequence_number: int | None
    data_age_seconds: float | None
    duplicate_count: int
    out_of_order_count: int
    sequence_jump_count: int
    last_health_transition: datetime

    @property
    def trusted(self) -> bool:
        return self.status is MarketDataHealthStatus.READY


@dataclass(frozen=True, slots=True)
class TickAssessment:
    accepted: bool
    trusted: bool
    reason: str
    health: InstrumentHealth


@dataclass(frozen=True, slots=True)
class Candle:
    exchange: Exchange
    symbol_token: str
    trading_symbol: str | None
    interval: CandleInterval
    start_at: datetime
    end_at: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    tick_count: int
    completed: bool
    trusted: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", Exchange(self.exchange))
        object.__setattr__(self, "interval", CandleInterval(self.interval))
        token = self.symbol_token.strip()
        if not token:
            raise ValueError("symbol_token must not be blank")
        object.__setattr__(self, "symbol_token", token)
        if self.trading_symbol is not None:
            symbol = self.trading_symbol.strip().upper()
            object.__setattr__(self, "trading_symbol", symbol or None)
        start = _aware(self.start_at, "start_at")
        end = _aware(self.end_at, "end_at")
        if end <= start:
            raise ValueError("candle end_at must be after start_at")
        if (end - start).total_seconds() != int(self.interval) * 60:
            raise ValueError("candle timestamps do not match interval")
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)
        prices: dict[str, Decimal] = {}
        for field_name in ("open_price", "high_price", "low_price", "close_price"):
            try:
                value = Decimal(str(getattr(self, field_name)))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"{field_name} must be a decimal") from exc
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{field_name} must be positive")
            prices[field_name] = value
            object.__setattr__(self, field_name, value)
        if prices["high_price"] < max(prices["open_price"], prices["close_price"]):
            raise ValueError("high_price is inconsistent with OHLC")
        if prices["low_price"] > min(prices["open_price"], prices["close_price"]):
            raise ValueError("low_price is inconsistent with OHLC")
        if self.volume < 0 or self.tick_count < 1:
            raise ValueError("candle volume/tick_count are invalid")


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(IST)
