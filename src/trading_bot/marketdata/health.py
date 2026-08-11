"""Epoch-aware tick freshness and sequence telemetry."""

from __future__ import annotations

from collections.abc import Mapping
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
    sequence_jumps: int
    recovery_ticks: int
    transition_at: datetime


class MarketDataHealthMonitor:
    """Track health without assuming per-instrument sequences are contiguous."""

    def __init__(
        self,
        *,
        stale_after_seconds: float,
        recovery_consecutive_ticks: int = 3,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if recovery_consecutive_ticks < 1:
            raise ValueError("recovery_consecutive_ticks must be positive")
        self._stale_after_seconds = stale_after_seconds
        self._recovery_required = recovery_consecutive_ticks
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
            state.sequence_jumps = 0
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
                sequence_jumps=0,
                recovery_ticks=0,
                transition_at=observed_at,
            )
            self._states[key] = state

        if (
            state.sequence is not None
            and tick.sequence_number == state.sequence
            and state.latest_exchange is not None
            and tick.exchange_timestamp == state.latest_exchange
        ):
            state.duplicates += 1
            return TickAssessment(
                False, False, "duplicate", self._snapshot(state, observed_at)
            )
        older_exchange_timestamp = (
            state.latest_exchange is not None
            and tick.exchange_timestamp < state.latest_exchange
        )
        if older_exchange_timestamp:
            state.out_of_order += 1
            self._transition(state, MarketDataHealthStatus.DEGRADED, observed_at)
            state.recovery_ticks = 0
            return TickAssessment(
                False,
                False,
                "out_of_order",
                self._snapshot(state, observed_at),
            )

        if (
            state.sequence is not None
            and tick.sequence_number - state.sequence > 1
        ):
            # SmartAPI sequences are sparse per token because one stream carries
            # interleaved instruments. Count jumps as telemetry only; they do not
            # demonstrate packet loss and must not affect trust.
            state.sequence_jumps += 1
        state.latest_exchange = tick.exchange_timestamp
        state.latest_received = tick.received_at
        state.sequence = tick.sequence_number

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

    def checkpoint_state(self) -> dict[str, object]:
        """Return a JSON-safe state used by the incremental projection."""
        return {
            "version": 1,
            "feed_epoch": self._epoch,
            "states": [
                {
                    "exchange": state.exchange.value,
                    "symbol_token": state.symbol_token,
                    "epoch": state.epoch,
                    "status": state.status.value,
                    "latest_exchange": _optional_iso(state.latest_exchange),
                    "latest_received": _optional_iso(state.latest_received),
                    "sequence": state.sequence,
                    "duplicates": state.duplicates,
                    "out_of_order": state.out_of_order,
                    "sequence_jumps": state.sequence_jumps,
                    "recovery_ticks": state.recovery_ticks,
                    "transition_at": state.transition_at.isoformat(),
                }
                for _, state in sorted(
                    self._states.items(),
                    key=lambda item: (item[0][0].value, item[0][1]),
                )
            ],
        }

    def restore_checkpoint_state(self, payload: Mapping[str, object]) -> None:
        """Restore a previously validated checkpoint without replaying its prefix."""
        if self._states or self._epoch:
            raise ValueError("health monitor must be empty before checkpoint restore")
        if payload.get("version") != 1:
            raise ValueError("unsupported health checkpoint version")
        epoch = _non_negative_int(payload.get("feed_epoch"), "feed_epoch")
        raw_states = payload.get("states")
        if not isinstance(raw_states, list):
            raise ValueError("health checkpoint states must be a list")
        restored: dict[tuple[Exchange, str], _State] = {}
        for raw in raw_states:
            if not isinstance(raw, Mapping):
                raise ValueError("health checkpoint state must be an object")
            exchange = Exchange(str(raw.get("exchange")))
            symbol_token = str(raw.get("symbol_token", "")).strip()
            if not symbol_token:
                raise ValueError("health checkpoint token must not be blank")
            key = (exchange, symbol_token)
            if key in restored:
                raise ValueError("health checkpoint contains duplicate instruments")
            state_epoch = _non_negative_int(raw.get("epoch"), "epoch")
            if state_epoch > epoch:
                raise ValueError("instrument epoch is ahead of feed epoch")
            restored[key] = _State(
                exchange=exchange,
                symbol_token=symbol_token,
                epoch=state_epoch,
                status=MarketDataHealthStatus(str(raw.get("status"))),
                latest_exchange=_optional_datetime(raw.get("latest_exchange")),
                latest_received=_optional_datetime(raw.get("latest_received")),
                sequence=_optional_non_negative_int(raw.get("sequence"), "sequence"),
                duplicates=_non_negative_int(raw.get("duplicates"), "duplicates"),
                out_of_order=_non_negative_int(
                    raw.get("out_of_order"), "out_of_order"
                ),
                sequence_jumps=_non_negative_int(
                    raw.get("sequence_jumps"), "sequence_jumps"
                ),
                recovery_ticks=_non_negative_int(
                    raw.get("recovery_ticks"), "recovery_ticks"
                ),
                transition_at=_required_datetime(
                    raw.get("transition_at"), "transition_at"
                ),
            )
        self._epoch = epoch
        self._states = restored

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
            sequence_jump_count=state.sequence_jumps,
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


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _required_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO timestamp")
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp") from exc


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _required_datetime(value, "timestamp")


def _non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _optional_non_negative_int(value: object, field_name: str) -> int | None:
    return None if value is None else _non_negative_int(value, field_name)
