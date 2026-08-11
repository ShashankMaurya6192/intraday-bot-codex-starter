from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal

import trading_bot.__main__ as cli
from trading_bot.domain import (
    IST,
    Exchange,
    FeedEvent,
    FeedEventType,
    MarketTick,
    SubscriptionMode,
)
from trading_bot.marketdata import (
    Candle,
    CandleInterval,
    MarketDataProvenance,
    MarketIntelligenceProjection,
    MarketReplayEngine,
    SessionCandleAggregator,
)
from trading_bot.storage import ProjectionMarketDataWriter, SQLiteMarketRecorder

SESSION = date(2026, 8, 11)
OPEN = datetime(2026, 8, 11, 9, 15, tzinfo=IST)


def _tick(
    exchange_offset: float,
    *,
    received_offset: float | None = None,
    sequence: int,
    token: str = "1",
    symbol: str = "ABC-EQ",
    price: str = "100",
    volume: int = 1_000,
) -> MarketTick:
    exchange_at = OPEN + timedelta(seconds=exchange_offset)
    received_at = OPEN + timedelta(
        seconds=(
            exchange_offset + 0.01
            if received_offset is None
            else received_offset
        )
    )
    last_price = Decimal(price)
    return MarketTick(
        exchange=Exchange.NSE_CM,
        symbol_token=token,
        trading_symbol=symbol,
        subscription_mode=SubscriptionMode.QUOTE,
        sequence_number=sequence,
        exchange_timestamp=exchange_at,
        received_at=received_at,
        last_traded_price=last_price,
        average_traded_price=Decimal("100"),
        volume_trade_for_the_day=volume,
        open_price=Decimal("99"),
        high_price=max(Decimal("101"), last_price),
        low_price=min(Decimal("98"), last_price),
        close_price=Decimal("99.5"),
    )


def _projection(recorder: SQLiteMarketRecorder) -> MarketIntelligenceProjection:
    return MarketIntelligenceProjection(
        recorder,
        session_date=SESSION,
        stale_after_seconds=300,
        recovery_consecutive_ticks=3,
    )


def _candle_semantics(candles) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            candle.exchange,
            candle.symbol_token,
            candle.interval,
            candle.start_at,
            candle.end_at,
            candle.open_price,
            candle.high_price,
            candle.low_price,
            candle.close_price,
            candle.volume,
            candle.tick_count,
            candle.trusted,
            candle.provenance,
        )
        for candle in sorted(
            candles,
            key=lambda item: (
                item.exchange.value,
                item.symbol_token,
                item.start_at,
                item.interval,
            ),
        )
    )


def _current_candles(recorder: SQLiteMarketRecorder) -> list[Candle]:
    return recorder.load_candles(
        start_at=datetime.combine(SESSION, datetime.min.time(), tzinfo=IST),
        end_at=datetime.combine(
            SESSION + timedelta(days=1), datetime.min.time(), tzinfo=IST
        ),
    )


def test_completed_snapshot_is_non_mutating() -> None:
    aggregator = SessionCandleAggregator()
    aggregator.process(_tick(10, sequence=1), trusted=True)
    aggregator.process(_tick(50, sequence=2), trusted=True)
    before = aggregator.checkpoint_state()

    snapshot = aggregator.snapshot_completed(until=OPEN + timedelta(minutes=1))

    assert len(snapshot) == 1
    assert snapshot[0].interval is CandleInterval.ONE_MINUTE
    assert snapshot[0].tick_count == 2
    assert aggregator.checkpoint_state() == before


def test_wall_clock_completion_is_ephemeral_and_late_tick_revises_view() -> None:
    first = _tick(10, sequence=1, price="100", volume=1_000)
    delayed = _tick(
        50,
        received_offset=65,
        sequence=2,
        price="101",
        volume=1_050,
    )
    later_same_bucket = _tick(
        55,
        received_offset=70,
        sequence=3,
        price="99",
        volume=1_070,
    )
    progression = _tick(
        70,
        received_offset=71,
        sequence=4,
        price="102",
        volume=1_100,
    )
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        projection = _projection(recorder)
        projection.initialize(now=OPEN, allow_rebuild=True)
        writer = ProjectionMarketDataWriter(recorder, projection)
        writer.record_ticks((first, delayed))

        assert _current_candles(recorder) == []
        before = recorder.load_projection_checkpoint(SESSION)
        first_view = recorder.load_projection_view(SESSION)
        after = recorder.load_projection_checkpoint(SESSION)
        first_minute = next(
            candle
            for candle in first_view.candles
            if candle.interval is CandleInterval.ONE_MINUTE
        )

        assert before == after
        assert first_minute.tick_count == 2
        assert first_minute.high_price == Decimal("101")
        assert first_minute.volume == 50
        assert _current_candles(recorder) == []

        writer.record_ticks((later_same_bucket,))
        revised = recorder.load_projection_view(SESSION)
        revised_minute = next(
            candle
            for candle in revised.candles
            if candle.interval is CandleInterval.ONE_MINUTE
        )
        assert revised_minute.tick_count == 3
        assert revised_minute.low_price == Decimal("99")
        assert revised_minute.volume == 70
        assert _current_candles(recorder) == []

        writer.record_ticks((progression,))
        durable = next(
            candle
            for candle in _current_candles(recorder)
            if candle.interval is CandleInterval.ONE_MINUTE
        )

    assert durable == revised_minute


def test_delayed_five_minute_and_multi_symbol_batches_match_replay() -> None:
    ticks: list[MarketTick] = []
    sequence = 1
    for minute in range(5):
        for token, symbol, price in (
            ("1", "ABC-EQ", str(100 + minute)),
            ("2", "XYZ-EQ", str(200 + minute)),
        ):
            exchange_offset = minute * 60 + (50 if minute == 4 else 10)
            received_offset = (
                305 + (0.1 if token == "2" else 0)
                if minute == 4
                else exchange_offset + (0.1 if token == "2" else 0.01)
            )
            ticks.append(
                _tick(
                    exchange_offset,
                    received_offset=received_offset,
                    sequence=sequence,
                    token=token,
                    symbol=symbol,
                    price=price,
                    volume=1_000 + minute * 100,
                )
            )
            sequence += 1
    ticks.sort(key=lambda item: item.received_at)
    late_suffix = (
        _tick(
            295,
            received_offset=310,
            sequence=sequence,
            token="1",
            price="106",
            volume=1_450,
        ),
        _tick(
            295,
            received_offset=310.1,
            sequence=sequence + 1,
            token="2",
            symbol="XYZ-EQ",
            price="206",
            volume=1_450,
        ),
    )
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        projection = _projection(recorder)
        projection.initialize(now=OPEN, allow_rebuild=True)
        writer = ProjectionMarketDataWriter(recorder, projection)
        writer.record_ticks(ticks)

        assert not any(
            candle.interval is CandleInterval.FIVE_MINUTES
            for candle in _current_candles(recorder)
        )
        first_view = recorder.load_projection_view(SESSION)
        first_five = {
            candle.symbol_token: candle
            for candle in first_view.candles
            if candle.interval is CandleInterval.FIVE_MINUTES
        }
        assert {token: candle.tick_count for token, candle in first_five.items()} == {
            "1": 5,
            "2": 5,
        }

        writer.record_ticks(late_suffix)
        view = recorder.load_projection_view(SESSION)
        replay = MarketReplayEngine(stale_after_seconds=300).replay(
            [*ticks, *late_suffix],
            evaluation_at=view.checkpoint.updated_at,
        )

    assert _candle_semantics(view.candles) == _candle_semantics(replay.candles)
    five = [
        candle
        for candle in view.candles
        if candle.interval is CandleInterval.FIVE_MINUTES
    ]
    assert len(five) == 2
    assert all(candle.tick_count == 6 for candle in five)


def test_restart_restores_working_candles_and_suffix_matches_replay() -> None:
    prefix = [
        _tick(10, sequence=1, volume=1_000),
        _tick(50, received_offset=65, sequence=2, price="101", volume=1_050),
    ]
    suffix = _tick(
        55,
        received_offset=70,
        sequence=3,
        price="99",
        volume=1_070,
    )
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        projection = _projection(recorder)
        projection.initialize(now=OPEN, allow_rebuild=True)
        writer = ProjectionMarketDataWriter(recorder, projection)
        writer.record_ticks(prefix)
        checkpoint = recorder.load_projection_checkpoint(SESSION)
        assert checkpoint is not None
        assert _current_candles(recorder) == []

        recorder.record_tick(suffix)
        restarted = _projection(recorder)
        update = restarted.initialize(
            now=suffix.received_at + timedelta(seconds=1),
            allow_rebuild=False,
        )
        view = recorder.load_projection_view(SESSION)
        replay = MarketReplayEngine(stale_after_seconds=300).replay(
            [*prefix, suffix], evaluation_at=view.checkpoint.updated_at
        )

    assert update.incremental_ticks_processed == 1
    assert not update.rebuilt
    assert view.checkpoint.last_tick_id == checkpoint.last_tick_id + 1
    assert _candle_semantics(view.candles) == _candle_semantics(replay.candles)


def test_old_projection_version_rebuild_preserves_raw_events_and_history(
    tmp_path,
) -> None:
    database = tmp_path / "projection-version.db"
    database_url = f"sqlite:///{database}"
    prior = Candle(
        exchange=Exchange.NSE_CM,
        symbol_token="1",
        trading_symbol="ABC-EQ",
        interval=CandleInterval.ONE_MINUTE,
        start_at=OPEN - timedelta(days=1),
        end_at=OPEN - timedelta(days=1) + timedelta(minutes=1),
        open_price=Decimal("98"),
        high_price=Decimal("99"),
        low_price=Decimal("97"),
        close_price=Decimal("98.5"),
        volume=1_000,
        tick_count=10,
        completed=True,
        trusted=True,
        provenance=MarketDataProvenance.ANGEL_HISTORICAL,
    )
    event = FeedEvent(
        FeedEventType.CONNECTED,
        OPEN,
        "connected",
    )
    ticks = (_tick(10, sequence=1), _tick(70, sequence=2, volume=1_100))
    with SQLiteMarketRecorder(database_url) as recorder:
        recorder.record_candles((prior,))
        recorder.record_feed_event(event)
        recorder.record_ticks(ticks)
        projection = _projection(recorder)
        projection.initialize(now=ticks[-1].received_at, allow_rebuild=True)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE market_projection_checkpoint SET state_version = 1"
        )
        connection.commit()

    with SQLiteMarketRecorder(database_url) as recorder:
        rebuilt = _projection(recorder)
        update = rebuilt.initialize(
            now=ticks[-1].received_at + timedelta(seconds=1),
            allow_rebuild=True,
        )
        checkpoint = recorder.load_projection_checkpoint(SESSION)
        stored_prior = recorder.load_candles(
            start_at=prior.start_at,
            end_at=prior.end_at,
        )

        assert recorder.count_ticks() == 2
        assert recorder.load_feed_events() == [event]

    assert update.rebuilt
    assert update.rebuild_reason == "corrupt_checkpoint"
    assert update.incremental_ticks_processed == 2
    assert checkpoint is not None and checkpoint.state_version == 2
    assert stored_prior == [prior]


def test_reconnect_candle_trust_matches_replay_without_retroactive_change() -> None:
    first_connect = FeedEvent(FeedEventType.CONNECTED, OPEN, "connected")
    reconnect = FeedEvent(
        FeedEventType.CONNECTED,
        OPEN + timedelta(seconds=70),
        "reconnected",
    )
    ticks = [
        _tick(10, sequence=1),
        _tick(60, sequence=2, volume=1_050),
        _tick(80, sequence=3, volume=1_060),
        _tick(90, sequence=4, volume=1_070),
        _tick(100, sequence=5, volume=1_080),
        _tick(120, sequence=6, volume=1_100),
    ]
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        projection = _projection(recorder)
        projection.initialize(now=OPEN, allow_rebuild=True)
        writer = ProjectionMarketDataWriter(recorder, projection)
        writer.record_feed_event(first_connect)
        writer.record_ticks(ticks[:2])
        writer.record_feed_event(reconnect)
        writer.record_ticks(ticks[2:])
        view = recorder.load_projection_view(SESSION)

    replay = MarketReplayEngine(
        stale_after_seconds=300,
        recovery_consecutive_ticks=3,
    ).replay(
        ticks,
        evaluation_at=view.checkpoint.updated_at,
        feed_epoch_starts=(reconnect.occurred_at,),
    )
    assert _candle_semantics(view.candles) == _candle_semantics(replay.candles)
    one_minute = {
        candle.start_at: candle
        for candle in view.candles
        if candle.interval is CandleInterval.ONE_MINUTE
    }
    assert one_minute[OPEN].trusted
    assert not one_minute[OPEN + timedelta(minutes=1)].trusted


def test_session_close_working_bucket_is_ephemeral_and_matches_replay() -> None:
    session_close_offset = (15 * 60 + 29 - (9 * 60 + 15)) * 60
    ticks = [
        _tick(session_close_offset + 10, sequence=1, volume=10_000),
        _tick(
            session_close_offset + 59,
            sequence=2,
            price="101",
            volume=10_100,
        ),
    ]
    after_close = OPEN.replace(hour=15, minute=31)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        projection = _projection(recorder)
        projection.initialize(now=OPEN, allow_rebuild=True)
        ProjectionMarketDataWriter(recorder, projection).record_ticks(ticks)
        assert _current_candles(recorder) == []

        projection.view(now=after_close)
        view = recorder.load_projection_view(SESSION)
        replay = MarketReplayEngine(stale_after_seconds=300).replay(
            ticks, evaluation_at=view.checkpoint.updated_at
        )

        assert _current_candles(recorder) == []

    assert _candle_semantics(view.candles) == _candle_semantics(replay.candles)
    assert {
        (candle.interval, candle.start_at)
        for candle in view.candles
    } == {
        (CandleInterval.ONE_MINUTE, OPEN.replace(hour=15, minute=29)),
        (CandleInterval.FIVE_MINUTES, OPEN.replace(hour=15, minute=25)),
    }


def test_candle_difference_is_bounded_and_reports_fields() -> None:
    projection: list[Candle] = []
    replay: list[Candle] = []
    for index in range(7):
        start_at = OPEN + timedelta(minutes=index)
        projection.append(
            Candle(
                exchange=Exchange.NSE_CM,
                symbol_token="1",
                trading_symbol="ABC-EQ",
                interval=CandleInterval.ONE_MINUTE,
                start_at=start_at,
                end_at=start_at + timedelta(minutes=1),
                open_price=Decimal("100"),
                high_price=Decimal("101"),
                low_price=Decimal("99"),
                close_price=Decimal("100"),
                volume=100,
                tick_count=2,
                completed=True,
                trusted=True,
            )
        )
        replay.append(
            Candle(
                exchange=Exchange.NSE_CM,
                symbol_token="1",
                trading_symbol="ABC-EQ",
                interval=CandleInterval.ONE_MINUTE,
                start_at=start_at,
                end_at=start_at + timedelta(minutes=1),
                open_price=Decimal("100"),
                high_price=Decimal("101"),
                low_price=Decimal("99"),
                close_price=Decimal("100.5"),
                volume=200,
                tick_count=3,
                completed=True,
                trusted=False,
            )
        )

    difference = cli._candle_difference(projection, replay)

    assert difference["changed_count"] == 7
    assert len(difference["changed_examples"]) == 5
    assert difference["only_projection_count"] == 0
    assert difference["only_replay_count"] == 0
    assert set(difference["changed_examples"][0]["fields"]) == {
        "close_price",
        "volume",
        "tick_count",
        "trusted",
    }
