"""Deterministic replay from recorded ticks into health state and candles."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol

from trading_bot.domain import IST, Exchange, FeedEvent, FeedEventType, MarketTick

from .candles import SessionCandleAggregator
from .health import MarketDataHealthMonitor
from .models import Candle, CandleInterval, InstrumentHealth


class ReplayStore(Protocol):
    def load_ticks(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        received_order: bool = False,
    ) -> list[MarketTick]: ...

    def record_candles(self, candles: Sequence[Candle]) -> int: ...

    def load_feed_events(self, *, limit: int | None = None) -> list[FeedEvent]: ...

    def record_volume_baseline_session(
        self,
        *,
        exchange: Exchange,
        symbol_token: str,
        session_date: date,
        cumulative_by_minute: dict[int, int],
        complete: bool,
        trusted: bool,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReplayResult:
    candles: tuple[Candle, ...]
    health: tuple[InstrumentHealth, ...]
    accepted_ticks: int
    rejected_ticks: int


class MarketReplayEngine:
    def __init__(
        self,
        *,
        stale_after_seconds: float,
        recovery_consecutive_ticks: int = 3,
        allowed_sequence_gap: int = 0,
    ) -> None:
        self._stale_after_seconds = stale_after_seconds
        self._recovery_ticks = recovery_consecutive_ticks
        self._allowed_gap = allowed_sequence_gap

    def replay(
        self,
        ticks: list[MarketTick],
        *,
        evaluation_at: datetime | None = None,
        feed_epoch_starts: Sequence[datetime] = (),
    ) -> ReplayResult:
        monitor = MarketDataHealthMonitor(
            stale_after_seconds=self._stale_after_seconds,
            recovery_consecutive_ticks=self._recovery_ticks,
            allowed_sequence_gap=self._allowed_gap,
        )
        aggregator = SessionCandleAggregator()
        candles: list[Candle] = []
        accepted = 0
        rejected = 0
        ordered_ticks = sorted(
            ticks,
            key=lambda tick: (
                tick.received_at,
                tick.exchange.value,
                tick.symbol_token,
                tick.sequence_number,
            ),
        )
        epoch_starts = sorted(_aware(value) for value in feed_epoch_starts)
        epoch_index = 0
        for tick in ordered_ticks:
            while (
                epoch_index < len(epoch_starts)
                and epoch_starts[epoch_index] <= tick.received_at
            ):
                monitor.begin_feed_epoch(at=epoch_starts[epoch_index])
                epoch_index += 1
            assessment = monitor.observe(tick, now=tick.received_at)
            if not assessment.accepted:
                rejected += 1
                continue
            accepted += 1
            candles.extend(aggregator.process(tick, trusted=assessment.trusted))

        if ordered_ticks:
            last_date = max(tick.exchange_timestamp.date() for tick in ordered_ticks)
            flush_at = datetime.combine(last_date, time(15, 30), tzinfo=IST)
            candles.extend(aggregator.flush(until=flush_at))
            health_at = evaluation_at or max(tick.received_at for tick in ordered_ticks)
            health = monitor.all_health(now=health_at)
        else:
            health = ()
        return ReplayResult(
            candles=tuple(
                sorted(
                    candles,
                    key=lambda item: (
                        item.exchange.value,
                        item.symbol_token,
                        item.start_at,
                        item.interval,
                    ),
                )
            ),
            health=health,
            accepted_ticks=accepted,
            rejected_ticks=rejected,
        )

    def replay_date(
        self,
        store: ReplayStore,
        session_date: date,
        *,
        evaluation_at: datetime | None = None,
        persist: bool = True,
    ) -> ReplayResult:
        start_at = datetime.combine(session_date, time.min, tzinfo=IST)
        end_at = start_at + timedelta(days=1)
        ticks = store.load_ticks(
            start_at=start_at,
            end_at=end_at,
            received_order=True,
        )
        connected_events = tuple(
            event.occurred_at
            for event in store.load_feed_events()
            if event.event_type is FeedEventType.CONNECTED
            and event.occurred_at.date() == session_date
        )
        # The first connection establishes the initial sequence domain. Only
        # subsequent connects represent a discontinuity for recorded ticks.
        epoch_starts = tuple(sorted(connected_events))[1:]
        result = self.replay(
            ticks,
            evaluation_at=evaluation_at,
            feed_epoch_starts=epoch_starts,
        )
        if persist:
            store.record_candles(result.candles)
            _persist_complete_baselines(store, result.candles, session_date)
        return result


def _persist_complete_baselines(
    store: ReplayStore, candles: tuple[Candle, ...], session_date: date
) -> None:
    grouped: dict[tuple[Exchange, str], list[Candle]] = {}
    for candle in candles:
        if candle.interval is CandleInterval.ONE_MINUTE:
            grouped.setdefault((candle.exchange, candle.symbol_token), []).append(
                candle
            )
    session_open = datetime.combine(session_date, time(9, 15), tzinfo=IST)
    for (exchange, token), items in grouped.items():
        items.sort(key=lambda item: item.start_at)
        cumulative = 0
        cumulative_by_minute: dict[int, int] = {}
        for item in items:
            cumulative += item.volume
            minute = int((item.start_at - session_open).total_seconds() // 60)
            cumulative_by_minute[minute] = cumulative
        complete = set(cumulative_by_minute) == set(range(375))
        trusted = all(item.trusted for item in items)
        store.record_volume_baseline_session(
            exchange=exchange,
            symbol_token=token,
            session_date=session_date,
            cumulative_by_minute=cumulative_by_minute,
            complete=complete,
            trusted=trusted,
        )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("feed epoch timestamp must be timezone-aware")
    return value.astimezone(IST)
