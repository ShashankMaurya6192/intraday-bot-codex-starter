"""Broker boundaries for authentication and market data only.

Order placement intentionally does not exist in this paper-only build.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from trading_bot.domain import IST, FeedEvent, MarketSubscription, MarketTick


class BrokerError(RuntimeError):
    """Base error raised by a broker adapter."""


class BrokerAuthenticationError(BrokerError):
    """Authentication failed or returned an incomplete session."""


class MarketDataConnectionError(BrokerError):
    """The market-data feed could not reach a trustworthy connected state."""


class BrokerReferenceDataError(BrokerError):
    """A broker reference-data request failed or returned an invalid envelope."""


class BrokerHistoricalDataError(BrokerReferenceDataError):
    """Base error for a sanitized historical-candle request failure."""

    def __init__(
        self,
        safe_message: str,
        *,
        broker_status: object | None = None,
        broker_message: str | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.broker_status = broker_status
        self.broker_message = broker_message
        self.error_code = error_code


class BrokerHistoricalRateLimitError(BrokerHistoricalDataError):
    """Angel rejected historical data because the access rate was exceeded."""


class BrokerHistoricalResponseError(BrokerHistoricalDataError):
    """Historical data returned a rejected or malformed broker response."""


class BrokerHistoricalTransportError(BrokerHistoricalDataError):
    """A retryable transport/server failure interrupted historical data."""


class TickNormalizationError(BrokerError):
    """A broker payload could not be converted into a valid domain tick."""


@dataclass(frozen=True, slots=True)
class BrokerSession:
    client_code: str = field(repr=False)
    authenticated_at: datetime
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    feed_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.authenticated_at.tzinfo is None
            or self.authenticated_at.utcoffset() is None
        ):
            raise ValueError("authenticated_at must be timezone-aware")
        object.__setattr__(
            self,
            "authenticated_at",
            self.authenticated_at.astimezone(IST),
        )
        for field_name in (
            "client_code",
            "access_token",
            "refresh_token",
            "feed_token",
        ):
            value = getattr(self, field_name).strip()
            if not value:
                raise ValueError(f"{field_name} must not be blank")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    max_attempts: int = 5
    initial_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 30.0
    stable_connection_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 0:
            raise ValueError("max_attempts must be non-negative")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("reconnect delays must be non-negative")
        if self.multiplier < 1:
            raise ValueError("reconnect multiplier must be at least 1")
        if self.initial_delay_seconds > self.max_delay_seconds:
            raise ValueError("initial reconnect delay cannot exceed maximum delay")
        if self.stable_connection_seconds <= 0:
            raise ValueError("stable_connection_seconds must be positive")

    def delay_for_attempt(self, attempt: int) -> float:
        """Return the capped delay before a one-based retry attempt."""
        if attempt < 1:
            raise ValueError("attempt must be at least 1")
        delay = self.initial_delay_seconds * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_delay_seconds)


TickHandler = Callable[[MarketTick], None]
FeedEventHandler = Callable[[FeedEvent], None]


class MarketDataBroker(ABC):
    """Blocking market-data feed contract implemented by broker adapters."""

    @abstractmethod
    def authenticate(self) -> BrokerSession:
        """Create and retain a broker session without exposing credentials."""

    @abstractmethod
    def stream(
        self,
        subscriptions: Sequence[MarketSubscription],
        on_tick: TickHandler,
        on_event: FeedEventHandler | None = None,
    ) -> None:
        """Connect and block until disconnected or retries are exhausted."""

    @abstractmethod
    def disconnect(self) -> None:
        """Request a clean shutdown of the active stream."""
