"""FIFO writer coupling authoritative raw storage to the scanner projection."""

from __future__ import annotations

from collections.abc import Iterable

from trading_bot.domain import FeedEvent, MarketTick
from trading_bot.marketdata.projection import MarketIntelligenceProjection

from .sqlite import SQLiteMarketRecorder


class ProjectionWriterError(RuntimeError):
    """Raw input is durable but derived state needs deterministic catch-up."""


class ProjectionMarketDataWriter:
    """Persist raw input first, then checkpoint its deterministic projection."""

    def __init__(
        self,
        recorder: SQLiteMarketRecorder,
        projection: MarketIntelligenceProjection,
    ) -> None:
        self._recorder = recorder
        self._projection = projection

    def record_ticks(self, ticks: Iterable[MarketTick]) -> int:
        inserted = self._recorder.record_ticks_with_ids(ticks)
        if inserted:
            try:
                self._projection.process_tick_records(inserted)
            except Exception as exc:
                raise ProjectionWriterError(
                    "raw ticks were stored but scanner projection failed; "
                    "restart suffix catch-up is required"
                ) from exc
        return len(inserted)

    def record_feed_event(self, event: FeedEvent) -> int:
        row_id = self._recorder.record_feed_event(event)
        try:
            self._projection.process_feed_event_record(row_id, event)
        except Exception as exc:
            raise ProjectionWriterError(
                "feed event was stored but scanner projection failed; "
                "restart suffix catch-up is required"
            ) from exc
        return row_id
