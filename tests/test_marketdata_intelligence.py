from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from trading_bot.broker import BrokerInstrument
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
from trading_bot.scanner import MarketScanner, ScannerReadiness
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


def test_health_tracks_stale_duplicate_timestamp_regression_and_recovery() -> None:
    monitor = MarketDataHealthMonitor(
        stale_after_seconds=5,
        recovery_consecutive_ticks=3,
    )
    first = monitor.observe(_tick(1, 0))
    assert first.health.status is MarketDataHealthStatus.READY

    duplicate = monitor.observe(_tick(1, 0))
    assert duplicate.accepted is False
    assert duplicate.reason == "duplicate"
    assert duplicate.health.duplicate_count == 1

    out_of_order = monitor.observe(
        _tick(4, -1), now=BASE + timedelta(seconds=2)
    )
    assert out_of_order.accepted is False
    assert out_of_order.health.out_of_order_count == 1
    assert out_of_order.health.status is MarketDataHealthStatus.DEGRADED

    recovering_jump = monitor.observe(_tick(4, 3))
    assert recovering_jump.accepted is True
    assert recovering_jump.trusted is False
    assert recovering_jump.reason == "recovering"
    assert recovering_jump.health.sequence_jump_count == 1

    assert monitor.observe(_tick(5, 4)).reason == "recovering"
    recovered = monitor.observe(_tick(6, 5))
    assert recovered.trusted is True
    assert recovered.health.status is MarketDataHealthStatus.READY

    stale = monitor.health(
        Exchange.NSE_CM,
        "1",
        now=BASE + timedelta(seconds=20),
    )
    assert stale.status is MarketDataHealthStatus.STALE
    assert stale.data_age_seconds == 15


def test_lower_sequence_with_later_exchange_timestamp_remains_trusted() -> None:
    monitor = MarketDataHealthMonitor(stale_after_seconds=5)

    first = monitor.observe(_tick(1200, 0))
    lower = monitor.observe(_tick(100, 1))

    assert first.trusted is True
    assert lower.accepted is True
    assert lower.trusted is True
    assert lower.reason == "healthy"
    assert lower.health.status is MarketDataHealthStatus.READY
    assert lower.health.sequence_number == 100
    assert lower.health.out_of_order_count == 0


def test_same_sequence_with_later_exchange_timestamp_is_not_a_duplicate() -> None:
    monitor = MarketDataHealthMonitor(stale_after_seconds=5)

    monitor.observe(_tick(100, 0))
    later = monitor.observe(_tick(100, 1))

    assert later.accepted is True
    assert later.trusted is True
    assert later.reason == "healthy"
    assert later.health.duplicate_count == 0


def test_sparse_increasing_smartapi_sequences_remain_trusted() -> None:
    monitor = MarketDataHealthMonitor(stale_after_seconds=5)

    assessments = [
        monitor.observe(_tick(sequence, offset))
        for offset, sequence in enumerate((100, 150, 892, 1200))
    ]

    assert all(item.accepted and item.trusted for item in assessments)
    assert all(item.reason == "healthy" for item in assessments)
    assert assessments[-1].health.status is MarketDataHealthStatus.READY
    assert assessments[-1].health.sequence_number == 1200
    assert assessments[-1].health.sequence_jump_count == 3
    assert assessments[-1].health.out_of_order_count == 0


def test_interleaved_six_symbol_smartapi_sequences_remain_trusted() -> None:
    monitor = MarketDataHealthMonitor(stale_after_seconds=5)
    assessments = []
    for round_number, sequence_base in enumerate((100, 150, 892, 1200)):
        for token_number in range(1, 7):
            assessments.append(
                monitor.observe(
                    _tick(
                        sequence_base + token_number - 1,
                        round_number,
                        token=str(token_number),
                    )
                )
            )

    assert all(item.accepted and item.trusted for item in assessments)
    health = monitor.all_health(now=BASE + timedelta(seconds=3, milliseconds=100))
    assert len(health) == 6
    assert all(item.status is MarketDataHealthStatus.READY for item in health)
    assert all(item.sequence_jump_count == 3 for item in health)
    assert all(item.out_of_order_count == 0 for item in health)


def test_health_models_reconnect_as_a_new_feed_epoch() -> None:
    monitor = MarketDataHealthMonitor(
        stale_after_seconds=5,
        recovery_consecutive_ticks=2,
    )
    monitor.observe(_tick(100, 0))
    before_reconnect = monitor.observe(_tick(150, 1))
    assert before_reconnect.health.sequence_jump_count == 1
    epoch = monitor.begin_feed_epoch(at=BASE + timedelta(seconds=1))

    assert epoch == 1
    first_after_reconnect = monitor.observe(_tick(1, 2))
    assert first_after_reconnect.reason == "recovering"
    assert first_after_reconnect.health.feed_epoch == 1
    assert first_after_reconnect.health.sequence_jump_count == 0
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


class _NoBaselines:
    def load_rvol_baselines(self, **_kwargs):
        return ()


def test_six_symbol_replay_does_not_report_sequence_jumps_as_untrusted() -> None:
    symbols = (
        ("1", "HDFCBANK-EQ"),
        ("2", "INFY-EQ"),
        ("3", "NIFTY 50"),
        ("4", "RELIANCE-EQ"),
        ("5", "SBIN-EQ"),
        ("6", "TCS-EQ"),
    )
    ticks = [
        _tick(
            sequence_base + token_index,
            round_number,
            token=token,
        )
        for round_number, sequence_base in enumerate((100, 900, 1600))
        for token_index, (token, _symbol) in enumerate(symbols)
    ]
    evaluation_at = BASE + timedelta(seconds=3)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_ticks(ticks)
        replay = MarketReplayEngine(stale_after_seconds=5).replay_date(
            recorder,
            BASE.date(),
            evaluation_at=evaluation_at,
            persist=False,
        )
    health_by = {
        (item.exchange, item.symbol_token): item for item in replay.health
    }
    candles_by = {
        (Exchange.NSE_CM, token): tuple(
            candle for candle in replay.candles if candle.symbol_token == token
        )
        for token, _symbol in symbols
    }
    equities = tuple(
        BrokerInstrument(Exchange.NSE_CM, token, symbol, Decimal("0.05"))
        for token, symbol in symbols
        if symbol != "NIFTY 50"
    )
    benchmark = BrokerInstrument(
        Exchange.NSE_CM,
        "3",
        "NIFTY 50",
        Decimal("0"),
        instrument_type="AMXIDX",
    )

    scan = MarketScanner().scan(
        instruments=equities,
        candles_by_instrument=candles_by,
        health_by_instrument=health_by,
        benchmark=benchmark,
        baseline_store=_NoBaselines(),
        now=evaluation_at,
        raw_ticks_replayed=replay.raw_ticks_replayed,
    )

    assert len(replay.health) == 6
    assert all(item.trusted for item in replay.health)
    assert all(item.sequence_jump_count == 2 for item in replay.health)
    assert scan.readiness is ScannerReadiness.NOT_READY
    assert scan.unhealthy_count == 0
    assert scan.diagnostics.unhealthy_market_data == ()
    assert all(
        "market_data_untrusted" not in message
        for message in scan.diagnostics.all_messages
    )
    assert scan.diagnostics.indicator_not_ready


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
