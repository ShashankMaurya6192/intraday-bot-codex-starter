"""Incremental, restart-safe projection of current-session market intelligence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol

from trading_bot.domain import IST, Exchange, FeedEvent, FeedEventType, MarketTick

from .candles import SessionCandleAggregator
from .health import MarketDataHealthMonitor
from .models import Candle, InstrumentHealth, SessionReference
from .recovery import (
    SessionReferenceAccumulator,
    opening_references_visible,
    recover_first_five_minute_candles,
)

PROJECTION_STATE_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProjectionCheckpoint:
    session_date: date
    last_tick_id: int
    last_feed_event_id: int
    last_received_at: datetime | None
    last_feed_event_at: datetime | None
    feed_epoch: int
    updated_at: datetime
    state_version: int
    engine_state: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.last_tick_id < 0 or self.last_feed_event_id < 0:
            raise ValueError("projection cursors must be non-negative")
        if self.feed_epoch < 0:
            raise ValueError("projection feed epoch must be non-negative")
        if self.state_version != PROJECTION_STATE_VERSION:
            raise ValueError("unsupported projection state version")
        object.__setattr__(self, "updated_at", _aware(self.updated_at))
        if self.last_received_at is not None:
            object.__setattr__(
                self, "last_received_at", _aware(self.last_received_at)
            )
        if self.last_feed_event_at is not None:
            object.__setattr__(
                self, "last_feed_event_at", _aware(self.last_feed_event_at)
            )
        object.__setattr__(self, "engine_state", dict(self.engine_state))


@dataclass(frozen=True, slots=True)
class ProjectionUpdate:
    incremental_ticks_processed: int
    incremental_feed_events_processed: int
    rebuilt: bool
    rebuild_reason: str | None


@dataclass(frozen=True, slots=True)
class ProjectionView:
    checkpoint: ProjectionCheckpoint
    candles: tuple[Candle, ...]
    health: tuple[InstrumentHealth, ...]
    session_references: tuple[SessionReference, ...]
    opening_range_references: tuple[SessionReference, ...]
    projection_age_seconds: float | None
    incremental_ticks_processed: int
    rebuilt: bool


class ProjectionError(RuntimeError):
    """Base error for materialized state that cannot be trusted."""


class ProjectionUnavailableError(ProjectionError):
    """The projection is missing and the caller did not authorize a rebuild."""


class ProjectionCheckpointAheadError(ProjectionError):
    """A checkpoint claims raw data that SQLite no longer contains."""


class ProjectionStateCorruptError(ProjectionError):
    """Persisted projection engine state cannot be safely restored."""


class ProjectionCatchUpLimitExceeded(ProjectionError):
    """A scan refused to consume an unexpectedly large raw-tick suffix."""

    def __init__(self, *, pending_ticks: int, limit: int) -> None:
        self.pending_ticks = pending_ticks
        self.limit = limit
        super().__init__(
            f"projection catch-up requires {pending_ticks} ticks; limit is {limit}"
        )


class ProjectionCutoffBehindCheckpointError(ProjectionError):
    """A requested as-of view predates already-materialized engine state."""


class ProjectionStore(Protocol):
    def load_projection_checkpoint(
        self, session_date: date
    ) -> ProjectionCheckpoint | None: ...

    def max_tick_id(self, session_date: date) -> int: ...

    def max_feed_event_id(self, session_date: date) -> int: ...

    def load_tick_records_after(
        self,
        session_date: date,
        *,
        after_id: int,
        received_at_or_before: datetime,
        limit: int | None = None,
    ) -> list[tuple[int, MarketTick]]: ...

    def load_feed_event_records_after(
        self,
        session_date: date,
        *,
        after_id: int,
        occurred_at_or_before: datetime,
    ) -> list[tuple[int, FeedEvent]]: ...

    def load_candles(
        self,
        *,
        exchange: Exchange | None = None,
        symbol_token: str | None = None,
        interval: object | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> list[Candle]: ...

    def save_projection_state(
        self,
        *,
        checkpoint: ProjectionCheckpoint,
        health: Sequence[InstrumentHealth],
        latest_references: Sequence[SessionReference],
        first_five_references: Sequence[SessionReference],
        opening_range_references: Sequence[SessionReference],
        candles: Sequence[Candle],
    ) -> None: ...

    def clear_projection_session(self, session_date: date) -> None: ...


class MarketIntelligenceProjection:
    """Materialize the same health/candle/recovery semantics as raw replay."""

    def __init__(
        self,
        store: ProjectionStore,
        *,
        session_date: date,
        stale_after_seconds: float,
        recovery_consecutive_ticks: int = 3,
    ) -> None:
        self._store = store
        self._session_date = session_date
        self._stale_after_seconds = stale_after_seconds
        self._recovery_ticks = recovery_consecutive_ticks
        self._reset_engine()
        self._checkpoint: ProjectionCheckpoint | None = None
        self._last_update = ProjectionUpdate(0, 0, False, None)

    @property
    def checkpoint(self) -> ProjectionCheckpoint | None:
        return self._checkpoint

    def initialize(
        self,
        *,
        now: datetime,
        allow_rebuild: bool,
        catch_up_tick_limit: int | None = None,
    ) -> ProjectionUpdate:
        """Restore a valid checkpoint and consume only its missing SQLite suffix."""
        evaluation = _aware(now)
        if catch_up_tick_limit is not None and catch_up_tick_limit < 1:
            raise ValueError("catch_up_tick_limit must be positive")
        try:
            checkpoint = self._store.load_projection_checkpoint(self._session_date)
        except (TypeError, ValueError) as exc:
            if not allow_rebuild:
                raise ProjectionStateCorruptError(
                    "projection checkpoint is corrupt"
                ) from exc
            return self._rebuild(evaluation, reason="corrupt_checkpoint")
        if checkpoint is None:
            if not allow_rebuild:
                raise ProjectionUnavailableError("projection checkpoint is missing")
            return self._rebuild(evaluation, reason="missing_checkpoint")
        self._validate_not_ahead(checkpoint)
        if any(
            timestamp is not None and timestamp > evaluation
            for timestamp in (
                checkpoint.updated_at,
                checkpoint.last_received_at,
                checkpoint.last_feed_event_at,
            )
        ):
            raise ProjectionCutoffBehindCheckpointError(
                "projection checkpoint is ahead of the requested as-of cutoff"
            )
        try:
            self._restore_engine(checkpoint.engine_state)
        except (KeyError, TypeError, ValueError) as exc:
            if not allow_rebuild:
                raise ProjectionStateCorruptError(
                    "projection engine state is corrupt"
                ) from exc
            return self._rebuild(evaluation, reason="corrupt_engine_state")
        if self._monitor.feed_epoch != checkpoint.feed_epoch:
            if not allow_rebuild:
                raise ProjectionStateCorruptError(
                    "projection feed epoch disagrees with engine state"
                )
            return self._rebuild(evaluation, reason="feed_epoch_mismatch")
        self._checkpoint = checkpoint
        update = self.catch_up(now=evaluation, tick_limit=catch_up_tick_limit)
        self._last_update = update
        return update

    def catch_up(
        self, *, now: datetime, tick_limit: int | None = None
    ) -> ProjectionUpdate:
        """Consume unprojected rows without loading the already-projected prefix."""
        evaluation = _aware(now)
        checkpoint = self._require_checkpoint()
        self._validate_not_ahead(checkpoint)
        requested_limit = None if tick_limit is None else tick_limit + 1
        ticks = self._store.load_tick_records_after(
            self._session_date,
            after_id=checkpoint.last_tick_id,
            received_at_or_before=evaluation,
            limit=requested_limit,
        )
        if tick_limit is not None and len(ticks) > tick_limit:
            raise ProjectionCatchUpLimitExceeded(
                pending_ticks=len(ticks), limit=tick_limit
            )
        events = self._store.load_feed_event_records_after(
            self._session_date,
            after_id=checkpoint.last_feed_event_id,
            occurred_at_or_before=evaluation,
        )
        self._process_merged(ticks, events)
        self._materialize(evaluation)
        update = ProjectionUpdate(len(ticks), len(events), False, None)
        self._last_update = update
        return update

    def process_tick_records(
        self, records: Sequence[tuple[int, MarketTick]]
    ) -> ProjectionUpdate:
        """Project newly committed live ticks in ingestion/FIFO order."""
        if not records:
            return ProjectionUpdate(0, 0, False, None)
        checkpoint = self._require_checkpoint()
        for row_id, tick in records:
            if row_id <= checkpoint.last_tick_id:
                raise ProjectionStateCorruptError(
                    "live tick cursor did not advance monotonically"
                )
            self._process_tick(row_id, tick)
            checkpoint = self._require_checkpoint()
        now = max(tick.received_at for _, tick in records)
        self._materialize(now)
        update = ProjectionUpdate(len(records), 0, False, None)
        self._last_update = update
        return update

    def process_feed_event_record(
        self, row_id: int, event: FeedEvent
    ) -> ProjectionUpdate:
        """Project one newly committed feed event in ingestion/FIFO order."""
        checkpoint = self._require_checkpoint()
        if row_id <= checkpoint.last_feed_event_id:
            raise ProjectionStateCorruptError(
                "live feed-event cursor did not advance monotonically"
            )
        self._process_event(row_id, event)
        self._materialize(event.occurred_at)
        update = ProjectionUpdate(0, 1, False, None)
        self._last_update = update
        return update

    def view(self, *, now: datetime) -> ProjectionView:
        evaluation = _aware(now)
        self._materialize(evaluation)
        checkpoint = self._require_checkpoint()
        age = None
        if checkpoint.last_received_at is not None:
            age = max(
                0.0,
                (evaluation - checkpoint.last_received_at).total_seconds(),
            )
        start_at, end_at = _session_bounds(self._session_date)
        return ProjectionView(
            checkpoint=checkpoint,
            candles=tuple(
                self._store.load_candles(start_at=start_at, end_at=end_at)
            ),
            health=self._monitor.all_health(now=evaluation),
            session_references=tuple(
                sorted(
                    self._references.latest.values(),
                    key=_reference_key,
                )
            ),
            opening_range_references=opening_references_visible(
                self._references.before_opening_range,
                evaluation_at=evaluation,
            ),
            projection_age_seconds=age,
            incremental_ticks_processed=(
                self._last_update.incremental_ticks_processed
            ),
            rebuilt=self._last_update.rebuilt,
        )

    def _rebuild(self, now: datetime, *, reason: str) -> ProjectionUpdate:
        self._store.clear_projection_session(self._session_date)
        self._reset_engine()
        self._checkpoint = self._new_checkpoint(now)
        ticks = self._store.load_tick_records_after(
            self._session_date,
            after_id=0,
            received_at_or_before=now,
        )
        events = self._store.load_feed_event_records_after(
            self._session_date,
            after_id=0,
            occurred_at_or_before=now,
        )
        self._process_merged(ticks, events)
        self._materialize(now)
        update = ProjectionUpdate(len(ticks), len(events), True, reason)
        self._last_update = update
        return update

    def _process_merged(
        self,
        ticks: Sequence[tuple[int, MarketTick]],
        events: Sequence[tuple[int, FeedEvent]],
    ) -> None:
        merged: list[tuple[datetime, int, int, object]] = [
            (event.occurred_at, 0, row_id, event) for row_id, event in events
        ]
        merged.extend(
            (tick.received_at, 1, row_id, tick) for row_id, tick in ticks
        )
        for _, kind, row_id, item in sorted(merged, key=lambda value: value[:3]):
            if kind == 0:
                if not isinstance(item, FeedEvent):
                    raise AssertionError("projection event merge type mismatch")
                self._process_event(row_id, item)
            else:
                if not isinstance(item, MarketTick):
                    raise AssertionError("projection tick merge type mismatch")
                self._process_tick(row_id, item)

    def _process_tick(self, row_id: int, tick: MarketTick) -> None:
        if tick.exchange_timestamp.date() != self._session_date:
            raise ProjectionStateCorruptError(
                "projection received a tick from another session"
            )
        assessment = self._monitor.observe(tick, now=tick.received_at)
        completed: tuple[Candle, ...] = ()
        if assessment.accepted:
            self._references.observe(tick, trusted=assessment.trusted)
            completed = self._aggregator.process(
                tick, trusted=assessment.trusted
            )
        checkpoint = self._require_checkpoint()
        self._checkpoint = ProjectionCheckpoint(
            session_date=self._session_date,
            last_tick_id=row_id,
            last_feed_event_id=checkpoint.last_feed_event_id,
            last_received_at=_latest(checkpoint.last_received_at, tick.received_at),
            last_feed_event_at=checkpoint.last_feed_event_at,
            feed_epoch=self._monitor.feed_epoch,
            updated_at=tick.received_at,
            state_version=PROJECTION_STATE_VERSION,
            engine_state={},
        )
        self._pending_candles.extend(completed)

    def _process_event(self, row_id: int, event: FeedEvent) -> None:
        if event.occurred_at.date() != self._session_date:
            raise ProjectionStateCorruptError(
                "projection received a feed event from another session"
            )
        if event.event_type is FeedEventType.CONNECTED:
            if self._connected_events_seen:
                self._monitor.begin_feed_epoch(at=event.occurred_at)
            self._connected_events_seen += 1
        checkpoint = self._require_checkpoint()
        self._checkpoint = ProjectionCheckpoint(
            session_date=self._session_date,
            last_tick_id=checkpoint.last_tick_id,
            last_feed_event_id=row_id,
            last_received_at=checkpoint.last_received_at,
            last_feed_event_at=_latest(
                checkpoint.last_feed_event_at, event.occurred_at
            ),
            feed_epoch=self._monitor.feed_epoch,
            updated_at=event.occurred_at,
            state_version=PROJECTION_STATE_VERSION,
            engine_state={},
        )

    def _materialize(self, now: datetime) -> None:
        evaluation = _aware(now)
        session_open, session_end = _session_bounds(self._session_date)
        if evaluation >= session_open:
            self._pending_candles.extend(
                self._aggregator.flush(until=min(evaluation, session_end))
            )
        persisted = self._store.load_candles(
            start_at=session_open,
            end_at=session_end,
        )
        persisted_by_identity = {
            (item.exchange, item.symbol_token, item.interval, item.start_at): item
            for item in persisted
        }
        by_identity = dict(persisted_by_identity)
        for candle in self._pending_candles:
            by_identity[
                (
                    candle.exchange,
                    candle.symbol_token,
                    candle.interval,
                    candle.start_at,
                )
            ] = candle
        recovered = recover_first_five_minute_candles(
            list(by_identity.values()),
            self._references.before_first_five,
            evaluation_at=evaluation,
        )
        changed_candles = tuple(
            item
            for item in recovered
            if persisted_by_identity.get(
                (item.exchange, item.symbol_token, item.interval, item.start_at)
            )
            != item
        )
        self._pending_candles.clear()
        checkpoint = self._require_checkpoint()
        health_now = _health_evaluation_at(checkpoint, evaluation)
        health = self._monitor.all_health(now=health_now)
        saved = ProjectionCheckpoint(
            session_date=self._session_date,
            last_tick_id=checkpoint.last_tick_id,
            last_feed_event_id=checkpoint.last_feed_event_id,
            last_received_at=checkpoint.last_received_at,
            last_feed_event_at=checkpoint.last_feed_event_at,
            feed_epoch=self._monitor.feed_epoch,
            updated_at=evaluation,
            state_version=PROJECTION_STATE_VERSION,
            engine_state=self._engine_state(),
        )
        self._store.save_projection_state(
            checkpoint=saved,
            health=health,
            latest_references=tuple(self._references.latest.values()),
            first_five_references=tuple(
                self._references.before_first_five.values()
            ),
            opening_range_references=tuple(
                self._references.before_opening_range.values()
            ),
            candles=changed_candles,
        )
        self._checkpoint = saved

    def _validate_not_ahead(self, checkpoint: ProjectionCheckpoint) -> None:
        max_tick_id = self._store.max_tick_id(self._session_date)
        max_event_id = self._store.max_feed_event_id(self._session_date)
        if (
            checkpoint.last_tick_id > max_tick_id
            or checkpoint.last_feed_event_id > max_event_id
        ):
            raise ProjectionCheckpointAheadError(
                "projection checkpoint is ahead of authoritative raw storage"
            )

    def _engine_state(self) -> dict[str, object]:
        return {
            "version": PROJECTION_STATE_VERSION,
            "health": self._monitor.checkpoint_state(),
            "candles": self._aggregator.checkpoint_state(),
            "latest_references": _references_state(self._references.latest),
            "first_five_references": _references_state(
                self._references.before_first_five
            ),
            "opening_range_references": _references_state(
                self._references.before_opening_range
            ),
            "connected_events_seen": self._connected_events_seen,
        }

    def _restore_engine(self, payload: Mapping[str, object]) -> None:
        if payload.get("version") != PROJECTION_STATE_VERSION:
            raise ValueError("unsupported projection engine version")
        raw_health = payload.get("health")
        raw_candles = payload.get("candles")
        if not isinstance(raw_health, Mapping) or not isinstance(
            raw_candles, Mapping
        ):
            raise ValueError("projection component state is missing")
        self._reset_engine()
        self._monitor.restore_checkpoint_state(raw_health)
        self._aggregator.restore_checkpoint_state(raw_candles)
        self._references.latest = _restore_references(
            payload.get("latest_references"), self._session_date
        )
        self._references.before_first_five = _restore_references(
            payload.get("first_five_references"), self._session_date
        )
        self._references.before_opening_range = _restore_references(
            payload.get("opening_range_references"), self._session_date
        )
        connected = payload.get("connected_events_seen")
        if (
            not isinstance(connected, int)
            or isinstance(connected, bool)
            or connected < 0
        ):
            raise ValueError("connected_events_seen must be non-negative")
        self._connected_events_seen = connected

    def _new_checkpoint(self, now: datetime) -> ProjectionCheckpoint:
        return ProjectionCheckpoint(
            session_date=self._session_date,
            last_tick_id=0,
            last_feed_event_id=0,
            last_received_at=None,
            last_feed_event_at=None,
            feed_epoch=0,
            updated_at=now,
            state_version=PROJECTION_STATE_VERSION,
            engine_state=self._engine_state(),
        )

    def _reset_engine(self) -> None:
        self._monitor = MarketDataHealthMonitor(
            stale_after_seconds=self._stale_after_seconds,
            recovery_consecutive_ticks=self._recovery_ticks,
        )
        self._aggregator = SessionCandleAggregator()
        self._references = SessionReferenceAccumulator()
        self._connected_events_seen = 0
        self._pending_candles: list[Candle] = []

    def _require_checkpoint(self) -> ProjectionCheckpoint:
        if self._checkpoint is None:
            raise ProjectionUnavailableError("projection is not initialized")
        return self._checkpoint


def _references_state(
    references: Mapping[tuple[Exchange, str], SessionReference],
) -> list[dict[str, object]]:
    return [
        {
            "exchange": item.exchange.value,
            "symbol_token": item.symbol_token,
            "trading_symbol": item.trading_symbol,
            "session_date": item.session_date.isoformat(),
            "observed_at": item.observed_at.isoformat(),
            "session_open": str(item.session_open),
            "session_high": str(item.session_high),
            "session_low": str(item.session_low),
            "last_traded_price": str(item.last_traded_price),
            "average_traded_price": (
                None
                if item.average_traded_price is None
                else str(item.average_traded_price)
            ),
            "cumulative_volume": item.cumulative_volume,
            "provenance": item.provenance.value,
            "trusted": item.trusted,
        }
        for item in sorted(references.values(), key=_reference_key)
    ]


def _restore_references(
    value: object, expected_date: date
) -> dict[tuple[Exchange, str], SessionReference]:
    from decimal import Decimal

    if not isinstance(value, list):
        raise ValueError("projection references must be a list")
    restored: dict[tuple[Exchange, str], SessionReference] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("projection reference must be an object")
        raw_date = raw.get("session_date")
        if (
            not isinstance(raw_date, str)
            or date.fromisoformat(raw_date) != expected_date
        ):
            raise ValueError("projection reference session date is invalid")
        average = raw.get("average_traded_price")
        reference = SessionReference(
            exchange=Exchange(str(raw.get("exchange"))),
            symbol_token=str(raw.get("symbol_token", "")),
            trading_symbol=(
                None
                if raw.get("trading_symbol") is None
                else str(raw.get("trading_symbol"))
            ),
            session_date=expected_date,
            observed_at=datetime.fromisoformat(str(raw.get("observed_at"))),
            session_open=Decimal(str(raw.get("session_open"))),
            session_high=Decimal(str(raw.get("session_high"))),
            session_low=Decimal(str(raw.get("session_low"))),
            last_traded_price=Decimal(str(raw.get("last_traded_price"))),
            average_traded_price=(
                None if average is None else Decimal(str(average))
            ),
            cumulative_volume=(
                None
                if raw.get("cumulative_volume") is None
                else int(str(raw.get("cumulative_volume")))
            ),
            provenance=str(raw.get("provenance")),
            trusted=raw.get("trusted") is True,
        )
        key = (reference.exchange, reference.symbol_token)
        if key in restored:
            raise ValueError("projection references contain duplicate instruments")
        restored[key] = reference
    return restored


def _session_bounds(session_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(session_date, time.min, tzinfo=IST)
    return start, start + timedelta(days=1)


def _health_evaluation_at(
    checkpoint: ProjectionCheckpoint, fallback: datetime
) -> datetime:
    values = [fallback]
    if checkpoint.last_received_at is not None:
        values.append(checkpoint.last_received_at)
    if checkpoint.last_feed_event_at is not None:
        values.append(checkpoint.last_feed_event_at)
    return max(values)


def _latest(current: datetime | None, candidate: datetime) -> datetime:
    candidate = _aware(candidate)
    return candidate if current is None else max(current, candidate)


def _reference_key(reference: SessionReference) -> tuple[str, str]:
    return reference.exchange.value, reference.symbol_token


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("projection timestamp must be timezone-aware")
    return value.astimezone(IST)
