"""Bounded asynchronous tick ingestion with fail-closed health semantics."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from enum import StrEnum
from queue import Empty, Full, Queue
from typing import Protocol, cast

from trading_bot.domain import MarketTick


class TickBatchWriter(Protocol):
    def record_ticks(self, ticks: Iterable[MarketTick]) -> int: ...


class TickIngestionState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    UNHEALTHY = "UNHEALTHY"


class TickIngestionError(RuntimeError):
    """Base error for an ingestion service that cannot safely accept ticks."""


class TickQueueFullError(TickIngestionError):
    """The bounded queue is full; the feed must stop instead of dropping a tick."""


class TickWriterError(TickIngestionError):
    """The dedicated batch writer failed."""


class TickDrainTimeoutError(TickIngestionError):
    """The writer did not drain within the configured shutdown bound."""


class TickIngestionUnhealthyError(TickIngestionError):
    """The ingestion service is not in a state safe for downstream use."""


UnhealthyHandler = Callable[[TickIngestionError], None]
MonotonicClock = Callable[[], float]
_STOP_SIGNAL = object()


class TickIngestionService:
    """Move SQLite writes off the WebSocket callback onto one bounded worker."""

    def __init__(
        self,
        writer: TickBatchWriter,
        *,
        queue_capacity: int = 10_000,
        batch_size: int = 250,
        flush_interval_seconds: float = 0.05,
        shutdown_timeout_seconds: float = 10.0,
        on_unhealthy: UnhealthyHandler | None = None,
        monotonic_clock: MonotonicClock = time.monotonic,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        if batch_size < 1 or batch_size > queue_capacity:
            raise ValueError("batch_size must be between 1 and queue_capacity")
        if flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        if flush_interval_seconds > shutdown_timeout_seconds:
            raise ValueError(
                "flush_interval_seconds cannot exceed shutdown_timeout_seconds"
            )

        self._writer = writer
        self._queue: Queue[MarketTick | object] = Queue(maxsize=queue_capacity)
        self._batch_size = batch_size
        self._flush_interval_seconds = flush_interval_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._on_unhealthy = on_unhealthy
        self._monotonic_clock = monotonic_clock

        self._state = TickIngestionState.CREATED
        self._state_lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._worker: threading.Thread | None = None
        self._failure: TickIngestionError | None = None
        self._failure_cause: BaseException | None = None

    @property
    def state(self) -> TickIngestionState:
        with self._state_lock:
            return self._state

    @property
    def healthy(self) -> bool:
        with self._state_lock:
            return self._state is TickIngestionState.RUNNING and self._failure is None

    @property
    def failure(self) -> TickIngestionError | None:
        with self._state_lock:
            return self._failure

    @property
    def queued_ticks(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        with self._state_lock:
            if self._state is not TickIngestionState.CREATED:
                raise RuntimeError("tick ingestion can only be started once")
            self._state = TickIngestionState.RUNNING
            worker = threading.Thread(
                target=self._run,
                name="sqlite-tick-writer",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def submit(self, tick: MarketTick) -> None:
        """Queue one tick without blocking the broker callback."""
        with self._state_lock:
            if self._state is not TickIngestionState.RUNNING or self._failure:
                failure = self._failure
                state = self._state
            else:
                failure = None
                state = self._state
                try:
                    self._queue.put_nowait(tick)
                    return
                except Full as exc:
                    queue_error = exc

        if failure is not None or state is not TickIngestionState.RUNNING:
            raise TickIngestionUnhealthyError(
                f"tick ingestion is not healthy (state={state.value})"
            ) from failure

        error = TickQueueFullError(
            "tick ingestion queue is full; feed is unsafe and must stop"
        )
        self._mark_unhealthy(error, queue_error)
        raise error from queue_error

    def ensure_healthy(self) -> None:
        """Fail if ticks must be suppressed because ingestion is not trustworthy."""
        with self._state_lock:
            if self._state is TickIngestionState.RUNNING and self._failure is None:
                return
            failure = self._failure
            state = self._state
        raise TickIngestionUnhealthyError(
            f"tick ingestion is not healthy (state={state.value})"
        ) from failure

    def stop(
        self,
        timeout_seconds: float | None = None,
        *,
        raise_on_failure: bool = True,
    ) -> None:
        """Stop accepting ticks and drain queued work within a bounded timeout."""
        timeout = (
            self._shutdown_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")

        with self._state_lock:
            if self._state is TickIngestionState.CREATED:
                self._state = TickIngestionState.STOPPED
                return
            if self._state is TickIngestionState.STOPPED:
                return
            if self._state is TickIngestionState.RUNNING:
                self._state = TickIngestionState.DRAINING
            should_wake_worker = self._state is TickIngestionState.DRAINING
            worker = self._worker
            self._stop_requested.set()
            if should_wake_worker:
                try:
                    self._queue.put_nowait(_STOP_SIGNAL)
                except Full:
                    # A full queue already guarantees that the worker will wake.
                    pass

        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                error = TickDrainTimeoutError(
                    "tick ingestion did not drain before the shutdown timeout"
                )
                self._mark_unhealthy(error)
                if raise_on_failure:
                    raise error
                return

        with self._state_lock:
            failure = self._failure
            failure_cause = self._failure_cause
            if failure is None:
                self._state = TickIngestionState.STOPPED
        if failure is not None and raise_on_failure:
            raise failure from failure_cause

    def _run(self) -> None:
        try:
            while not (self._stop_requested.is_set() and self._queue.empty()):
                batch = self._take_batch()
                if not batch:
                    continue
                try:
                    self._writer.record_ticks(batch)
                except Exception as exc:
                    error = TickWriterError(
                        "SQLite tick writer failed; ingestion is unsafe"
                    )
                    self._mark_unhealthy(error, exc)
                    return
                finally:
                    for _ in batch:
                        self._queue.task_done()
        finally:
            with self._state_lock:
                if self._state is TickIngestionState.DRAINING and self._failure is None:
                    self._state = TickIngestionState.STOPPED

    def _take_batch(self) -> list[MarketTick]:
        try:
            first = self._queue.get(timeout=self._flush_interval_seconds)
        except Empty:
            return []
        if first is _STOP_SIGNAL:
            self._queue.task_done()
            return []

        batch = [cast(MarketTick, first)]
        deadline = self._monotonic_clock() + self._flush_interval_seconds
        while len(batch) < self._batch_size:
            if self._stop_requested.is_set():
                timeout = 0.0
            else:
                timeout = max(0.0, deadline - self._monotonic_clock())
            try:
                item = self._queue.get(timeout=timeout)
            except Empty:
                break
            if item is _STOP_SIGNAL:
                self._queue.task_done()
                break
            batch.append(cast(MarketTick, item))
        return batch

    def _mark_unhealthy(
        self,
        error: TickIngestionError,
        cause: BaseException | None = None,
    ) -> None:
        with self._state_lock:
            if self._failure is not None:
                return
            self._failure = error
            self._failure_cause = cause
            self._state = TickIngestionState.UNHEALTHY
            self._stop_requested.set()
            handler = self._on_unhealthy
        if handler is not None:
            try:
                handler(error)
            except Exception:
                # The original ingestion failure remains authoritative.
                pass
