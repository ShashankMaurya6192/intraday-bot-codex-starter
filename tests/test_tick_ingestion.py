from __future__ import annotations

import threading
from datetime import datetime
from decimal import Decimal

import pytest

from trading_bot.domain import (
    IST,
    Exchange,
    FeedEvent,
    FeedEventType,
    MarketTick,
    SubscriptionMode,
)
from trading_bot.storage import (
    SQLiteMarketRecorder,
    TickDrainTimeoutError,
    TickIngestionService,
    TickIngestionState,
    TickIngestionUnhealthyError,
    TickQueueFullError,
    TickWriterError,
)


def _tick(sequence_number: int) -> MarketTick:
    return MarketTick(
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        subscription_mode=SubscriptionMode.QUOTE,
        sequence_number=sequence_number,
        exchange_timestamp=datetime(2026, 8, 10, 9, 30, 5, tzinfo=IST),
        received_at=datetime(2026, 8, 10, 9, 30, 5, 10_000, tzinfo=IST),
        last_traded_price=Decimal("812.25"),
    )


class RecordingBatchWriter:
    def __init__(self) -> None:
        self.batches: list[list[MarketTick]] = []

    def record_ticks(self, ticks) -> int:
        batch = list(ticks)
        self.batches.append(batch)
        return len(batch)


def test_ingestion_batches_ticks_on_dedicated_writer() -> None:
    writer = RecordingBatchWriter()
    service = TickIngestionService(
        writer,
        queue_capacity=10,
        batch_size=5,
        flush_interval_seconds=0.05,
        shutdown_timeout_seconds=1,
    )
    service.start()

    for sequence in range(5):
        service.submit(_tick(sequence))
    service.stop()

    assert [[tick.sequence_number for tick in batch] for batch in writer.batches] == [
        [0, 1, 2, 3, 4]
    ]
    assert service.state is TickIngestionState.STOPPED


def test_clean_shutdown_drains_all_queued_ticks() -> None:
    writer = RecordingBatchWriter()
    service = TickIngestionService(
        writer,
        queue_capacity=20,
        batch_size=3,
        flush_interval_seconds=0.01,
        shutdown_timeout_seconds=1,
    )
    service.start()

    for sequence in range(8):
        service.submit(_tick(sequence))
    service.stop()

    written = [tick.sequence_number for batch in writer.batches for tick in batch]
    assert written == list(range(8))
    assert service.queued_ticks == 0


def test_queue_overflow_marks_unhealthy_and_never_fails_silently() -> None:
    entered_writer = threading.Event()
    release_writer = threading.Event()
    unhealthy: list[Exception] = []

    class BlockingWriter(RecordingBatchWriter):
        def record_ticks(self, ticks) -> int:
            batch = list(ticks)
            self.batches.append(batch)
            entered_writer.set()
            assert release_writer.wait(1)
            return len(batch)

    writer = BlockingWriter()
    service = TickIngestionService(
        writer,
        queue_capacity=1,
        batch_size=1,
        flush_interval_seconds=0.01,
        shutdown_timeout_seconds=1,
        on_unhealthy=unhealthy.append,
    )
    service.start()
    service.submit(_tick(1))
    assert entered_writer.wait(1)
    service.submit(_tick(2))

    with pytest.raises(TickQueueFullError, match="queue is full"):
        service.submit(_tick(3))

    assert service.state is TickIngestionState.UNHEALTHY
    assert isinstance(service.failure, TickQueueFullError)
    assert len(unhealthy) == 1
    with pytest.raises(TickIngestionUnhealthyError):
        service.ensure_healthy()

    release_writer.set()
    with pytest.raises(TickQueueFullError):
        service.stop()


def test_writer_failure_marks_unhealthy_and_stops_accepting_ticks() -> None:
    failure_reported = threading.Event()

    class FailingWriter:
        def record_ticks(self, _ticks) -> int:
            raise OSError("database unavailable")

    service = TickIngestionService(
        FailingWriter(),
        queue_capacity=4,
        batch_size=2,
        flush_interval_seconds=0.01,
        shutdown_timeout_seconds=1,
        on_unhealthy=lambda _error: failure_reported.set(),
    )
    service.start()
    service.submit(_tick(1))

    assert failure_reported.wait(1)
    assert service.state is TickIngestionState.UNHEALTHY
    assert isinstance(service.failure, TickWriterError)
    with pytest.raises(TickIngestionUnhealthyError):
        service.submit(_tick(2))
    with pytest.raises(TickWriterError) as exc_info:
        service.stop()
    assert isinstance(exc_info.value.__cause__, OSError)


def test_duplicate_ticks_remain_idempotent_through_ingestion() -> None:
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        service = TickIngestionService(
            recorder,
            queue_capacity=10,
            batch_size=5,
            flush_interval_seconds=0.01,
            shutdown_timeout_seconds=1,
        )
        service.start()
        service.submit(_tick(1))
        service.submit(_tick(1))
        service.submit(_tick(2))
        service.stop()

        assert recorder.count_ticks() == 2


def test_shutdown_timeout_marks_ingestion_unhealthy() -> None:
    entered_writer = threading.Event()
    release_writer = threading.Event()

    class StuckWriter:
        def record_ticks(self, ticks) -> int:
            batch = list(ticks)
            entered_writer.set()
            assert release_writer.wait(1)
            return len(batch)

    service = TickIngestionService(
        StuckWriter(),
        queue_capacity=2,
        batch_size=1,
        flush_interval_seconds=0.01,
        shutdown_timeout_seconds=0.01,
    )
    service.start()
    service.submit(_tick(1))
    assert entered_writer.wait(1)

    with pytest.raises(TickDrainTimeoutError):
        service.stop()

    release_writer.set()
    service.stop(timeout_seconds=1, raise_on_failure=False)
    assert service.state is TickIngestionState.UNHEALTHY


def test_ticks_and_feed_events_share_one_fifo_writer_path() -> None:
    order: list[str] = []

    class MarketDataWriter:
        def record_ticks(self, ticks) -> int:
            batch = list(ticks)
            order.extend(f"tick:{item.sequence_number}" for item in batch)
            return len(batch)

        def record_feed_event(self, event: FeedEvent) -> int:
            order.append(f"event:{event.event_type.value}")
            return 1

    service = TickIngestionService(
        MarketDataWriter(),
        queue_capacity=10,
        batch_size=10,
        flush_interval_seconds=0.01,
        shutdown_timeout_seconds=1,
    )
    service.start()
    service.submit(_tick(1))
    service.submit_feed_event(
        FeedEvent(
            event_type=FeedEventType.CONNECTED,
            occurred_at=datetime(2026, 8, 10, 9, 30, 6, tzinfo=IST),
            message="connected",
        )
    )
    service.submit(_tick(2))
    service.stop()

    assert order == ["tick:1", "event:CONNECTED", "tick:2"]
