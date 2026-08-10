from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from trading_bot.domain import (
    IST,
    Exchange,
    FeedEvent,
    FeedEventType,
    MarketTick,
    SubscriptionMode,
)
from trading_bot.marketdata import (
    CandleInterval,
    MarketDataHealthMonitor,
    MarketDataHealthStatus,
    MarketReplayEngine,
    SessionCandleAggregator,
)
from trading_bot.storage import SQLiteMarketRecorder

BASE = datetime(2026, 8, 10, 9, 15, tzinfo=IST)


def _tick(
    sequence: int,
    seconds: int,
    *,
    price: str = "100",
    volume: int | None = 100,
    token: str = "1",
) -> MarketTick:
    exchange_at = BASE + timedelta(seconds=seconds)
    return MarketTick(
        exchange=Exchange.NSE_CM,
        symbol_token=token,
        trading_symbol="ABC-EQ" if token == "1" else "NIFTY",
        subscription_mode=SubscriptionMode.QUOTE,
        sequence_number=sequence,
        exchange_timestamp=exchange_at,
        received_at=exchange_at + timedelta(milliseconds=100),
        last_traded_price=Decimal(price),
        volume_trade_for_the_day=volume,
    )


def test_health_tracks_stale_duplicate_out_of_order_gap_and_recovery() -> None:
    monitor = MarketDataHealthMonitor(
        stale_after_seconds=5,
        recovery_consecutive_ticks=3,
    )
    first = monitor.observe(_tick(1, 0))
    assert first.health.status is MarketDataHealthStatus.READY

    duplicate = monitor.observe(_tick(1, 1))
    assert duplicate.accepted is False
    assert duplicate.reason == "duplicate"
    assert duplicate.health.duplicate_count == 1

    out_of_order = monitor.observe(_tick(0, 2))
    assert out_of_order.accepted is False
    assert out_of_order.health.out_of_order_count == 1
    assert out_of_order.health.status is MarketDataHealthStatus.DEGRADED

    gap = monitor.observe(_tick(4, 3))
    assert gap.accepted is True
    assert gap.trusted is False
    assert gap.health.sequence_gap_count == 1

    assert monitor.observe(_tick(5, 4)).reason == "recovering"
    assert monitor.observe(_tick(6, 5)).reason == "recovering"
    recovered = monitor.observe(_tick(7, 6))
    assert recovered.trusted is True
    assert recovered.health.status is MarketDataHealthStatus.READY

    stale = monitor.health(
        Exchange.NSE_CM,
        "1",
        now=BASE + timedelta(seconds=20),
    )
    assert stale.status is MarketDataHealthStatus.STALE
    assert stale.data_age_seconds == 14


def test_health_models_reconnect_as_a_new_feed_epoch() -> None:
    monitor = MarketDataHealthMonitor(
        stale_after_seconds=5,
        recovery_consecutive_ticks=2,
    )
    monitor.observe(_tick(100, 0))
    epoch = monitor.begin_feed_epoch(at=BASE + timedelta(seconds=1))

    assert epoch == 1
    first_after_reconnect = monitor.observe(_tick(1, 2))
    assert first_after_reconnect.reason == "recovering"
    assert first_after_reconnect.health.feed_epoch == 1
    recovered = monitor.observe(_tick(2, 3))
    assert recovered.reason == "healthy"
    assert recovered.trusted is True


def test_replay_applies_explicit_reconnect_epochs_and_recovery() -> None:
    engine = MarketReplayEngine(
        stale_after_seconds=5,
        recovery_consecutive_ticks=2,
    )
    result = engine.replay(
        [_tick(100, 0), _tick(1, 2), _tick(2, 3)],
        feed_epoch_starts=(BASE + timedelta(seconds=1),),
    )

    assert result.health[0].feed_epoch == 1
    assert result.health[0].status is MarketDataHealthStatus.READY
    assert result.accepted_ticks == 3
    assert any(not candle.trusted for candle in result.candles)


def test_stale_tick_is_never_trusted() -> None:
    monitor = MarketDataHealthMonitor(stale_after_seconds=2)
    tick = _tick(1, 0)
    assessment = monitor.observe(tick, now=BASE + timedelta(seconds=10))

    assert assessment.accepted is True
    assert assessment.trusted is False
    assert assessment.health.status is MarketDataHealthStatus.STALE


def test_one_and_five_minute_boundaries_and_cumulative_volume_reset() -> None:
    aggregator = SessionCandleAggregator()
    completed = []
    completed.extend(aggregator.process(_tick(1, 0, volume=100), trusted=True))
    completed.extend(
        aggregator.process(_tick(2, 30, price="101", volume=110), trusted=True)
    )
    completed.extend(
        aggregator.process(_tick(3, 60, price="102", volume=120), trusted=True)
    )
    completed.extend(
        aggregator.process(_tick(4, 300, price="103", volume=150), trusted=True)
    )

    one_minute = [
        candle
        for candle in completed
        if candle.interval is CandleInterval.ONE_MINUTE and candle.start_at == BASE
    ][0]
    five_minute = [
        candle for candle in completed if candle.interval is CandleInterval.FIVE_MINUTES
    ][0]
    assert (one_minute.open_price, one_minute.high_price, one_minute.close_price) == (
        Decimal("100"),
        Decimal("101"),
        Decimal("101"),
    )
    assert one_minute.volume == 10
    assert one_minute.end_at == BASE + timedelta(minutes=1)
    assert five_minute.start_at == BASE
    assert five_minute.end_at == BASE + timedelta(minutes=5)
    assert five_minute.volume == 20

    reset = SessionCandleAggregator(intervals=(CandleInterval.ONE_MINUTE,))
    reset.process(_tick(1, 0, volume=100), trusted=True)
    reset.process(_tick(2, 10, volume=90), trusted=True)
    reset.process(_tick(3, 20, volume=95), trusted=True)
    candles = reset.process(_tick(4, 60, volume=100), trusted=True)
    assert candles[0].volume == 5


def test_out_of_order_tick_does_not_mutate_completed_candle() -> None:
    monitor = MarketDataHealthMonitor(stale_after_seconds=5)
    aggregator = SessionCandleAggregator(intervals=(CandleInterval.ONE_MINUTE,))
    for tick in (_tick(1, 0, price="100"), _tick(2, 30, price="101")):
        assessment = monitor.observe(tick)
        aggregator.process(tick, trusted=assessment.trusted)

    late = _tick(3, 10, price="999")
    assessment = monitor.observe(late)
    assert assessment.accepted is False
    completed = aggregator.process(_tick(4, 60, price="102"), trusted=False)

    assert completed[0].close_price == Decimal("101")
    assert completed[0].high_price == Decimal("101")


def test_replay_is_deterministic_and_candle_persistence_is_idempotent() -> None:
    ticks = [
        _tick(1, 0, volume=100),
        _tick(2, 30, price="101", volume=110),
        _tick(3, 60, price="102", volume=120),
        _tick(4, 300, price="103", volume=150),
    ]
    engine = MarketReplayEngine(stale_after_seconds=5)

    first = engine.replay(ticks)
    second = engine.replay(ticks)

    assert first == second
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        assert recorder.record_candles(first.candles) == len(first.candles)
        assert recorder.record_candles(second.candles) == 0
        assert recorder.load_candles() == list(first.candles)


def test_first_recorded_connection_establishes_initial_epoch_without_degrading() -> (
    None
):
    engine = MarketReplayEngine(stale_after_seconds=5)
    session_date = BASE.date()
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_feed_event(
            FeedEvent(
                event_type=FeedEventType.CONNECTED,
                occurred_at=BASE - timedelta(seconds=1),
                message="initial connection",
            )
        )
        recorder.record_ticks([_tick(1, 0), _tick(2, 60)])

        result = engine.replay_date(recorder, session_date, persist=False)

    assert result.health[0].feed_epoch == 0
    assert all(candle.trusted for candle in result.candles)


def test_rvol_baseline_store_excludes_incomplete_or_degraded_sessions() -> None:
    session_date = date(2026, 8, 7)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        assert (
            recorder.record_volume_baseline_session(
                exchange=Exchange.NSE_CM,
                symbol_token="1",
                session_date=session_date,
                cumulative_by_minute={0: 100, 1: 150},
                complete=False,
                trusted=True,
            )
            is False
        )
        assert not recorder.load_rvol_baselines(
            exchange=Exchange.NSE_CM,
            symbol_token="1",
            before_date=date(2026, 8, 10),
            minute_offset=1,
            lookback_sessions=10,
        )
        assert recorder.record_volume_baseline_session(
            exchange=Exchange.NSE_CM,
            symbol_token="1",
            session_date=session_date,
            cumulative_by_minute={0: 100, 1: 150},
            complete=True,
            trusted=True,
        )
        assert recorder.load_rvol_baselines(
            exchange=Exchange.NSE_CM,
            symbol_token="1",
            before_date=date(2026, 8, 10),
            minute_offset=1,
            lookback_sessions=10,
        ) == (150,)
