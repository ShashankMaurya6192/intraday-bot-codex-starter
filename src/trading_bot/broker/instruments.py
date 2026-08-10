"""Fail-closed boundaries for the future cached Angel instrument master."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from trading_bot.domain import Exchange, MarketSubscription


class InstrumentMasterError(RuntimeError):
    """A cached broker instrument cannot be trusted or resolved."""


class InstrumentMasterUnavailableError(InstrumentMasterError):
    """No valid last-known-good local instrument master is available."""


class InstrumentNotFoundError(InstrumentMasterError):
    """The requested token/exchange pair does not exist in the cached master."""


class InstrumentMismatchError(InstrumentMasterError):
    """Configured instrument identity conflicts with the cached broker record."""


@dataclass(frozen=True, slots=True)
class BrokerInstrument:
    exchange: Exchange
    symbol_token: str
    trading_symbol: str
    tick_size: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", Exchange(self.exchange))
        token = self.symbol_token.strip()
        symbol = self.trading_symbol.strip()
        if not token.isdigit():
            raise ValueError("broker symbol_token must contain only digits")
        if not symbol:
            raise ValueError("trading_symbol must not be blank")
        try:
            tick_size = Decimal(str(self.tick_size))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("tick_size must be a valid decimal") from exc
        if not tick_size.is_finite() or tick_size <= 0:
            raise ValueError("tick_size must be finite and positive")
        object.__setattr__(self, "symbol_token", token)
        object.__setattr__(self, "trading_symbol", symbol)
        object.__setattr__(self, "tick_size", tick_size)


class CachedInstrumentMaster(Protocol):
    """Read only from a validated last-known-good local snapshot.

    Implementations added in Phase 2 must never download the full instrument
    master on the trading path. Missing, stale, or corrupt cache state must
    raise an InstrumentMasterError rather than substitute guessed tokens.
    """

    @property
    def cache_path(self) -> Path: ...

    @property
    def loaded_at(self) -> datetime: ...

    def require(self, subscription: MarketSubscription) -> BrokerInstrument: ...


class IntradayAllowedUniverse(Protocol):
    """Broker-provided current NSE intraday eligibility boundary for Phase 2."""

    def is_intraday_allowed(self, instrument: BrokerInstrument) -> bool: ...


def validate_subscription_against_instrument(
    subscription: MarketSubscription,
    instrument: BrokerInstrument,
) -> BrokerInstrument:
    """Validate configured identity against an authoritative cached record."""
    if subscription.exchange is not instrument.exchange:
        raise InstrumentMismatchError("subscription exchange does not match cache")
    if subscription.symbol_token != instrument.symbol_token:
        raise InstrumentMismatchError("subscription token does not match cache")
    if (
        subscription.trading_symbol is not None
        and subscription.trading_symbol != instrument.trading_symbol
    ):
        raise InstrumentMismatchError(
            "subscription trading symbol does not match cache"
        )
    return instrument
