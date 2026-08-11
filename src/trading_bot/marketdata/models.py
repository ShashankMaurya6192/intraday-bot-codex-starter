"""Broker-neutral market-data health and candle models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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


class MarketDataProvenance(StrEnum):
    LOCAL_TICKS = "LOCAL_TICKS"
    ANGEL_SESSION_FIELDS = "ANGEL_SESSION_FIELDS"
    ANGEL_HISTORICAL = "ANGEL_HISTORICAL"
    YFINANCE = "YFINANCE"


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
class SessionReference:
    """Validated as-of session fields carried by one trusted market tick."""

    exchange: Exchange
    symbol_token: str
    trading_symbol: str | None
    session_date: date
    observed_at: datetime
    session_open: Decimal
    session_high: Decimal
    session_low: Decimal
    last_traded_price: Decimal
    average_traded_price: Decimal | None
    cumulative_volume: int | None
    provenance: MarketDataProvenance
    trusted: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", Exchange(self.exchange))
        object.__setattr__(self, "provenance", MarketDataProvenance(self.provenance))
        token = self.symbol_token.strip()
        if not token:
            raise ValueError("symbol_token must not be blank")
        object.__setattr__(self, "symbol_token", token)
        if self.trading_symbol is not None:
            symbol = self.trading_symbol.strip().upper()
            object.__setattr__(self, "trading_symbol", symbol or None)
        observed_at = _aware(self.observed_at, "observed_at")
        if observed_at.date() != self.session_date:
            raise ValueError("session reference date does not match observed_at")
        object.__setattr__(self, "observed_at", observed_at)
        prices: dict[str, Decimal] = {}
        for field_name in (
            "session_open",
            "session_high",
            "session_low",
            "last_traded_price",
        ):
            try:
                value = Decimal(str(getattr(self, field_name)))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"{field_name} must be a decimal") from exc
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{field_name} must be positive and finite")
            prices[field_name] = value
            object.__setattr__(self, field_name, value)
        if prices["session_high"] < max(
            prices["session_open"], prices["last_traded_price"]
        ):
            raise ValueError("session_high is inconsistent with session prices")
        if prices["session_low"] > min(
            prices["session_open"], prices["last_traded_price"]
        ):
            raise ValueError("session_low is inconsistent with session prices")
        if self.average_traded_price is not None:
            average = Decimal(str(self.average_traded_price))
            if not average.is_finite() or average <= 0:
                raise ValueError("average_traded_price must be positive and finite")
            object.__setattr__(self, "average_traded_price", average)
        if self.cumulative_volume is not None and self.cumulative_volume < 0:
            raise ValueError("cumulative_volume must be non-negative")


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
    provenance: MarketDataProvenance = MarketDataProvenance.LOCAL_TICKS

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", Exchange(self.exchange))
        object.__setattr__(self, "interval", CandleInterval(self.interval))
        object.__setattr__(self, "provenance", MarketDataProvenance(self.provenance))
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
