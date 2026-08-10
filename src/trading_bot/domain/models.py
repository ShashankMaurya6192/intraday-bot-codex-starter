"""Broker-neutral domain models used by the market-data pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class TradingMode(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class Exchange(StrEnum):
    NSE_CM = "NSE_CM"
    NSE_FO = "NSE_FO"
    BSE_CM = "BSE_CM"
    BSE_FO = "BSE_FO"
    MCX_FO = "MCX_FO"
    NCX_FO = "NCX_FO"
    CDE_FO = "CDE_FO"


class SubscriptionMode(StrEnum):
    LTP = "LTP"
    QUOTE = "QUOTE"
    SNAP_QUOTE = "SNAP_QUOTE"
    DEPTH = "DEPTH"


class FeedEventType(StrEnum):
    AUTHENTICATED = "AUTHENTICATED"
    CONNECTED = "CONNECTED"
    SUBSCRIBED = "SUBSCRIBED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"


def _aware_in_ist(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(IST)


def _decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal") from exc
    if not converted.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return converted


@dataclass(frozen=True, slots=True)
class MarketSubscription:
    exchange: Exchange
    symbol_token: str
    trading_symbol: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", Exchange(self.exchange))
        token = self.symbol_token.strip()
        if not token:
            raise ValueError("symbol_token must not be blank")
        object.__setattr__(self, "symbol_token", token)

        if self.trading_symbol is not None:
            symbol = self.trading_symbol.strip()
            object.__setattr__(self, "trading_symbol", symbol or None)


@dataclass(frozen=True, slots=True)
class MarketTick:
    exchange: Exchange
    symbol_token: str
    subscription_mode: SubscriptionMode
    sequence_number: int
    exchange_timestamp: datetime
    received_at: datetime
    last_traded_price: Decimal
    trading_symbol: str | None = None
    last_traded_quantity: int | None = None
    average_traded_price: Decimal | None = None
    volume_trade_for_the_day: int | None = None
    total_buy_quantity: Decimal | None = None
    total_sell_quantity: Decimal | None = None
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    close_price: Decimal | None = None
    raw_payload: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", Exchange(self.exchange))
        object.__setattr__(
            self,
            "subscription_mode",
            SubscriptionMode(self.subscription_mode),
        )
        token = self.symbol_token.strip()
        if not token:
            raise ValueError("symbol_token must not be blank")
        if self.sequence_number < 0:
            raise ValueError("sequence_number must be non-negative")

        last_price = _decimal(self.last_traded_price, "last_traded_price")
        if last_price <= 0:
            raise ValueError("last_traded_price must be positive")

        object.__setattr__(self, "symbol_token", token)
        object.__setattr__(
            self,
            "exchange_timestamp",
            _aware_in_ist(self.exchange_timestamp, "exchange_timestamp"),
        )
        object.__setattr__(
            self, "received_at", _aware_in_ist(self.received_at, "received_at")
        )
        object.__setattr__(self, "last_traded_price", last_price)

        if self.trading_symbol is not None:
            symbol = self.trading_symbol.strip()
            object.__setattr__(self, "trading_symbol", symbol or None)

        for field_name in (
            "average_traded_price",
            "total_buy_quantity",
            "total_sell_quantity",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
        ):
            value = getattr(self, field_name)
            if value is not None:
                converted = _decimal(value, field_name)
                if converted < 0:
                    raise ValueError(f"{field_name} must be non-negative")
                object.__setattr__(self, field_name, converted)

        for field_name in ("last_traded_quantity", "volume_trade_for_the_day"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} must be non-negative")

        object.__setattr__(self, "raw_payload", dict(self.raw_payload))


@dataclass(frozen=True, slots=True)
class FeedEvent:
    event_type: FeedEventType
    occurred_at: datetime
    message: str
    details: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", FeedEventType(self.event_type))
        message = self.message.strip()
        if not message:
            raise ValueError("message must not be blank")
        object.__setattr__(self, "message", message)
        object.__setattr__(
            self, "occurred_at", _aware_in_ist(self.occurred_at, "occurred_at")
        )
        object.__setattr__(self, "details", dict(self.details))
