"""Epoch-aware tick freshness and sequence monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_bot.domain import IST, Exchange, MarketTick

from .models import (
    InstrumentHealth,
    MarketDataHealthStatus,
    TickAssessment,
)


@dataclass(slots=True)
class _State:
    exchange: Exchange
    symbol_token: str
    epoch: int
    status: MarketDataHealthStatus
    latest_exchange: datetime | None
    latest_received: datetime | None
    sequence: int | None
    duplicates: int
    out_of_order: int
    gaps: int
    recovery_ticks: int
    transition_at: datetime


class MarketDataHealthMonitor:
    """Track broker-neutral health without assuming sequences span reconnects."""

    def __init__(
        self,
        *,
        stale_after_seconds: float,
        recovery_consecutive_ticks: int = 3,
        allowed_sequence_gap: int = 0,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if recovery_consecutive_ticks < 1:
            raise ValueError("recovery_consecutive_ticks must be positive")
        if allowed_sequence_gap < 0:
            raise ValueError("allowed_sequence_gap must be non-negative")
        self._stale_after_seconds = stale_after_seconds
        self._recovery_required = recovery_consecutive_ticks
        self._allowed_gap = allowed_sequence_gap
        self._epoch = 0
        self._states: dict[tuple[Exchange, str], _State] = {}

    @property
    def feed_epoch(self) -> int:
        return self._epoch

    def begin_feed_epoch(self, *, at: datetime) -> int:
        """Start a new feed epoch and distrust prior sequence continuity."""
        timestamp = _aware(at)
        self._epoch += 1
        for state in self._states.values():
            state.epoch = self._epoch
            state.sequence = None
            state.recovery_ticks = 0
            self._transition(state, MarketDataHealthStatus.DEGRADED, timestamp)
        return self._epoch

    def observe(
        self, tick: MarketTick, *, now: datetime | None = None
    ) -> TickAssessment:
        observed_at = _aware(now or tick.received_at)
        key = (tick.exchange, tick.symbol_token)
        state = self._states.get(key)
        if state is None:
            initial_status = (
                MarketDataHealthStatus.READY
                if self._epoch == 0
                else MarketDataHealthStatus.DEGRADED
            )
            state = _State(
                exchange=tick.exchange,
                symbol_token=tick.symbol_token,
                epoch=self._epoch,
                status=initial_status,
                latest_exchange=None,
                latest_received=None,
                sequence=None,
                duplicates=0,
                out_of_order=0,
                gaps=0,
                recovery_ticks=0,
                transition_at=observed_at,
            )
            self._states[key] = state

        if state.sequence is not None and tick.sequence_number == state.sequence:
            state.duplicates += 1
            return TickAssessment(
                False, False, "duplicate", self._snapshot(state, observed_at)
            )
        if (
            state.sequence is not None
            and tick.sequence_number < state.sequence
            or state.latest_exchange is not None
            and tick.exchange_timestamp < state.latest_exchange
        ):
            state.out_of_order += 1
            self._transition(state, MarketDataHealthStatus.DEGRADED, observed_at)
            state.recovery_ticks = 0
            return TickAssessment(
                False,
                False,
                "out_of_order",
                self._snapshot(state, observed_at),
            )

        gap = 0
        if state.sequence is not None:
            gap = tick.sequence_number - state.sequence - 1
        state.latest_exchange = tick.exchange_timestamp
        state.latest_received = tick.received_at
        state.sequence = tick.sequence_number

        if gap > self._allowed_gap:
            state.gaps += 1
            state.recovery_ticks = 0
            self._transition(state, MarketDataHealthStatus.DEGRADED, observed_at)
            return TickAssessment(
                True,
                False,
                "sequence_gap",
                self._snapshot(state, observed_at),
            )

        age = max(0.0, (observed_at - tick.exchange_timestamp).total_seconds())
        if age > self._stale_after_seconds:
            state.recovery_ticks = 0
            self._transition(state, MarketDataHealthStatus.STALE, observed_at)
            return TickAssessment(
                True, False, "stale", self._snapshot(state, observed_at)
            )

        if state.status is MarketDataHealthStatus.DEGRADED:
            state.recovery_ticks += 1
            if state.recovery_ticks < self._recovery_required:
                return TickAssessment(
                    True,
                    False,
                    "recovering",
                    self._snapshot(state, observed_at),
                )
        self._transition(state, MarketDataHealthStatus.READY, observed_at)
        return TickAssessment(True, True, "healthy", self._snapshot(state, observed_at))

    def health(
        self,
        exchange: Exchange,
        symbol_token: str,
        *,
        now: datetime,
    ) -> InstrumentHealth:
        try:
            state = self._states[(exchange, symbol_token)]
        except KeyError as exc:
            raise KeyError("instrument has no observed market data") from exc
        return self._snapshot(state, _aware(now))

    def all_health(self, *, now: datetime) -> tuple[InstrumentHealth, ...]:
        timestamp = _aware(now)
        return tuple(
            self._snapshot(state, timestamp)
            for _, state in sorted(
                self._states.items(), key=lambda item: (item[0][0].value, item[0][1])
            )
        )

    def _snapshot(self, state: _State, now: datetime) -> InstrumentHealth:
        age = None
        if state.latest_exchange is not None:
            age = max(0.0, (now - state.latest_exchange).total_seconds())
            if age > self._stale_after_seconds:
                self._transition(state, MarketDataHealthStatus.STALE, now)
        return InstrumentHealth(
            exchange=state.exchange,
            symbol_token=state.symbol_token,
            feed_epoch=state.epoch,
            status=state.status,
            latest_exchange_timestamp=state.latest_exchange,
            latest_received_timestamp=state.latest_received,
            sequence_number=state.sequence,
            data_age_seconds=age,
            duplicate_count=state.duplicates,
            out_of_order_count=state.out_of_order,
            sequence_gap_count=state.gaps,
            last_health_transition=state.transition_at,
        )

    @staticmethod
    def _transition(
        state: _State, status: MarketDataHealthStatus, at: datetime
    ) -> None:
        if state.status is not status:
            state.status = status
            state.transition_at = at


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(IST)
