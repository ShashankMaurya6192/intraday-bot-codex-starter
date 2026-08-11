from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import trading_bot.__main__ as cli
from trading_bot.broker import BrokerInstrument
from trading_bot.config import Settings
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
    MarketDataHealthStatus,
    MarketDataProvenance,
    MarketIntelligenceProjection,
    MarketReplayEngine,
    ProjectionCatchUpPendingError,
    ProjectionCheckpointAheadError,
    ProjectionConcurrentAdvanceError,
    ProjectionCutoffBehindCheckpointError,
    ProjectionStateCorruptError,
)
from trading_bot.scanner import MarketScanner
from trading_bot.storage import (
    ProjectionMarketDataWriter,
    ProjectionWriterError,
    SQLiteMarketRecorder,
)

SESSION = date(2026, 8, 10)
OPEN = datetime(2026, 8, 10, 9, 15, tzinfo=IST)


def _tick(
    minute: int,
    *,
    second: int = 0,
    sequence: int | None = None,
    token: str = "1",
    symbol: str = "ABC-EQ",
    price: Decimal = Decimal("101"),
    volume: int = 1_000,
    average: Decimal | None = Decimal("100.5"),
) -> MarketTick:
    timestamp = OPEN + timedelta(minutes=minute, seconds=second)
    return MarketTick(
        exchange=Exchange.NSE_CM,
        symbol_token=token,
        trading_symbol=symbol,
        subscription_mode=SubscriptionMode.QUOTE,
        sequence_number=sequence if sequence is not None else minute * 100 + second,
        exchange_timestamp=timestamp,
        received_at=timestamp + timedelta(milliseconds=10),
        last_traded_price=price,
        average_traded_price=average,
        volume_trade_for_the_day=volume,
        open_price=Decimal("100"),
        high_price=max(Decimal("102"), price),
        low_price=min(Decimal("99"), price),
        close_price=Decimal("100"),
    )


def _event(event_type: FeedEventType, minute: int, second: int = 0) -> FeedEvent:
    return FeedEvent(
        event_type=event_type,
        occurred_at=OPEN + timedelta(minutes=minute, seconds=second),
        message=event_type.value,
    )


def _projection(
    recorder: SQLiteMarketRecorder,
    *,
    stale_after_seconds: float = 60,
    recovery_ticks: int = 3,
) -> MarketIntelligenceProjection:
    return MarketIntelligenceProjection(
        recorder,
        session_date=SESSION,
        stale_after_seconds=stale_after_seconds,
        recovery_consecutive_ticks=recovery_ticks,
    )


def test_initial_projection_persists_health_candles_and_session_reference() -> None:
    ticks = [_tick(minute, volume=1_000 + minute * 10) for minute in range(6)]
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_ticks(ticks)
        projection = _projection(recorder)
        update = projection.initialize(
            now=OPEN + timedelta(minutes=5, seconds=1),
            allow_rebuild=True,
        )
        view = projection.view(now=OPEN + timedelta(minutes=5, seconds=1))

        persisted_health = recorder.load_projection_health(SESSION)
        persisted_references = recorder.load_projection_references(
            SESSION, reference_kind="LATEST"
        )

    assert update.rebuilt
    assert update.incremental_ticks_processed == 6
    assert view.checkpoint.last_tick_id == 6
    assert view.health[0].status is MarketDataHealthStatus.READY
    assert persisted_health[0].sequence_number == ticks[-1].sequence_number
    assert persisted_references[0].last_traded_price == ticks[-1].last_traded_price
    assert any(
        candle.interval is CandleInterval.ONE_MINUTE for candle in view.candles
    )
    assert any(
        candle.interval is CandleInterval.FIVE_MINUTES for candle in view.candles
    )


def test_late_start_first_five_recovery_persists_without_fake_minutes() -> None:
    proof = _tick(
        4,
        second=59,
        price=Decimal("100.8"),
        volume=12_345,
        average=Decimal("100.4"),
    )
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_tick(proof)
        projection = _projection(recorder)
        projection.initialize(now=OPEN + timedelta(minutes=5), allow_rebuild=True)
        view = projection.view(now=OPEN + timedelta(minutes=5))

    one_minute_starts = {
        candle.start_at
        for candle in view.candles
        if candle.interval is CandleInterval.ONE_MINUTE
    }
    recovered = [
        candle
        for candle in view.candles
        if candle.interval is CandleInterval.FIVE_MINUTES
        and candle.start_at == OPEN
    ]
    assert one_minute_starts == {OPEN + timedelta(minutes=4)}
    assert len(recovered) == 1
    assert recovered[0].provenance is MarketDataProvenance.ANGEL_SESSION_FIELDS
    assert recovered[0].volume == 12_345


def test_pre_opening_range_reference_persists_and_becomes_visible_at_0930() -> None:
    proof = _tick(14, second=59, price=Decimal("101.5"), volume=5_000)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_tick(proof)
        projection = _projection(recorder)
        projection.initialize(now=OPEN + timedelta(minutes=15), allow_rebuild=True)
        view = projection.view(now=OPEN + timedelta(minutes=15))
        persisted = recorder.load_projection_references(
            SESSION, reference_kind="BEFORE_OPENING_RANGE"
        )

    assert len(persisted) == 1
    assert view.opening_range_references == tuple(persisted)
    assert view.opening_range_references[0].session_high == Decimal("102")


def test_reconnect_epoch_degrades_and_recovers_exactly_as_replay() -> None:
    first_connect = _event(FeedEventType.CONNECTED, 0)
    reconnect = _event(FeedEventType.CONNECTED, 1)
    ticks = [
        _tick(0, second=1, sequence=100),
        _tick(1, second=1, sequence=5),
        _tick(1, second=2, sequence=50),
        _tick(1, second=3, sequence=900),
    ]
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_feed_event(first_connect)
        recorder.record_ticks((ticks[0],))
        recorder.record_feed_event(reconnect)
        recorder.record_ticks(ticks[1:])
        projection = _projection(recorder)
        projection.initialize(
            now=OPEN + timedelta(minutes=1, seconds=4),
            allow_rebuild=True,
        )
        view = projection.view(now=OPEN + timedelta(minutes=1, seconds=4))

    replay = MarketReplayEngine(
        stale_after_seconds=60,
        recovery_consecutive_ticks=3,
    ).replay(
        ticks,
        evaluation_at=OPEN + timedelta(minutes=1, seconds=4),
        feed_epoch_starts=(reconnect.occurred_at,),
    )
    assert _health_semantics(view.health) == _health_semantics(replay.health)
    assert view.health[0].feed_epoch == 1
    assert view.health[0].status is MarketDataHealthStatus.READY


def test_checkpoint_advances_monotonically_through_live_writer() -> None:
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        projection = _projection(recorder)
        projection.initialize(now=OPEN, allow_rebuild=True)
        writer = ProjectionMarketDataWriter(recorder, projection)

        writer.record_ticks((_tick(0, second=1),))
        first = recorder.load_projection_checkpoint(SESSION)
        writer.record_ticks((_tick(0, second=2),))
        second = recorder.load_projection_checkpoint(SESSION)

    assert first is not None and second is not None
    assert first.last_tick_id == 1
    assert second.last_tick_id == 2
    assert second.last_received_at > first.last_received_at


def test_restart_restores_engine_and_catches_up_only_suffix(monkeypatch) -> None:
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_tick(_tick(0, second=1))
        initial = _projection(recorder)
        initial.initialize(now=OPEN + timedelta(seconds=2), allow_rebuild=True)
        checkpoint = recorder.load_projection_checkpoint(SESSION)
        assert checkpoint is not None
        recorder.record_tick(_tick(0, second=2))

        calls: list[int] = []
        original = recorder.load_tick_records_after

        def observed_load(
            session_date, *, after_id, received_at_or_before, limit=None
        ):
            calls.append(after_id)
            return original(
                session_date,
                after_id=after_id,
                received_at_or_before=received_at_or_before,
                limit=limit,
            )

        monkeypatch.setattr(recorder, "load_tick_records_after", observed_load)
        restarted = _projection(recorder)
        update = restarted.initialize(
            now=OPEN + timedelta(seconds=3),
            allow_rebuild=False,
        )
        view = restarted.view(now=OPEN + timedelta(seconds=3))

    assert calls == [checkpoint.last_tick_id]
    assert update.incremental_ticks_processed == 1
    assert not update.rebuilt
    assert view.checkpoint.last_tick_id == 2


def test_suffix_catch_up_respects_received_at_as_of_cutoff() -> None:
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_tick(_tick(0, second=1))
        initial = _projection(recorder)
        initial.initialize(now=OPEN + timedelta(seconds=2), allow_rebuild=True)
        recorder.record_tick(_tick(0, second=10))

        restarted = _projection(recorder)
        hidden = restarted.initialize(
            now=OPEN + timedelta(seconds=5),
            allow_rebuild=False,
        )
        hidden_checkpoint = restarted.checkpoint
        visible = restarted.catch_up(now=OPEN + timedelta(seconds=11))
        visible_checkpoint = restarted.checkpoint

    assert hidden.incremental_ticks_processed == 0
    assert hidden_checkpoint is not None
    assert hidden_checkpoint.last_tick_id == 1
    assert visible.incremental_ticks_processed == 1
    assert visible_checkpoint is not None
    assert visible_checkpoint.last_tick_id == 2


def test_checkpoint_ahead_of_raw_database_fails_closed(tmp_path) -> None:
    database = tmp_path / "projection.db"
    database_url = f"sqlite:///{database}"
    with SQLiteMarketRecorder(database_url) as recorder:
        recorder.record_tick(_tick(0, second=1))
        projection = _projection(recorder)
        projection.initialize(now=OPEN + timedelta(seconds=2), allow_rebuild=True)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE market_projection_checkpoint SET last_tick_id = 999"
        )
        connection.commit()

    with SQLiteMarketRecorder(database_url) as recorder:
        with pytest.raises(ProjectionCheckpointAheadError):
            _projection(recorder).initialize(
                now=OPEN + timedelta(seconds=3),
                allow_rebuild=True,
            )


def test_corrupt_checkpoint_rebuild_is_deterministic(tmp_path) -> None:
    database = tmp_path / "projection.db"
    database_url = f"sqlite:///{database}"
    ticks = (_tick(0, second=1), _tick(0, second=2))
    with SQLiteMarketRecorder(database_url) as recorder:
        recorder.record_ticks(ticks)
        projection = _projection(recorder)
        projection.initialize(now=OPEN + timedelta(seconds=3), allow_rebuild=True)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE market_projection_checkpoint SET state_json = '{broken'"
        )
        connection.commit()

    with SQLiteMarketRecorder(database_url) as recorder:
        with pytest.raises(ProjectionStateCorruptError):
            _projection(recorder).initialize(
                now=OPEN + timedelta(seconds=3), allow_rebuild=False
            )
        rebuilt = _projection(recorder)
        update = rebuilt.initialize(
            now=OPEN + timedelta(seconds=3), allow_rebuild=True
        )
        view = rebuilt.view(now=OPEN + timedelta(seconds=3))

    assert update.rebuilt
    assert update.rebuild_reason == "corrupt_checkpoint"
    assert update.incremental_ticks_processed == 2
    assert view.checkpoint.last_tick_id == 2


def test_projection_health_becomes_stale_at_view_cutoff() -> None:
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_tick(_tick(0, second=1))
        projection = _projection(recorder, stale_after_seconds=5)
        projection.initialize(now=OPEN + timedelta(seconds=2), allow_rebuild=True)
        view = projection.view(now=OPEN + timedelta(seconds=7))

    assert view.projection_age_seconds == pytest.approx(5.99)
    assert view.health[0].status is MarketDataHealthStatus.STALE


def test_incremental_projection_matches_deterministic_replay() -> None:
    ticks = [
        _tick(minute, second=59, sequence=100 + minute * 137, volume=1_000 + minute)
        for minute in range(21)
    ]
    evaluation = OPEN + timedelta(minutes=21)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_ticks(ticks)
        projection = _projection(recorder, stale_after_seconds=120)
        projection.initialize(now=evaluation, allow_rebuild=True)
        view = projection.view(now=evaluation)

    replay = MarketReplayEngine(stale_after_seconds=120).replay(
        ticks, evaluation_at=evaluation
    )
    assert _health_semantics(view.health) == _health_semantics(replay.health)
    assert _candle_semantics(view.candles) == _candle_semantics(replay.candles)
    assert view.session_references == replay.session_references
    assert view.opening_range_references == replay.opening_range_references


def test_projection_and_replay_produce_identical_qualification() -> None:
    stock = BrokerInstrument(
        Exchange.NSE_CM, "1", "ABC-EQ", Decimal("0.05")
    )
    benchmark = BrokerInstrument(
        Exchange.NSE_CM,
        "999",
        "NIFTY",
        Decimal("0.05"),
        instrument_type="AMXIDX",
    )
    ticks: list[MarketTick] = []
    for minute in range(21):
        stock_price = (
            Decimal("100") + Decimal(minute) / Decimal("100")
            if minute < 15
            else Decimal("101") + Decimal(minute - 15) / Decimal("10")
        )
        ticks.extend(
            (
                _tick(
                    minute,
                    sequence=100 + minute * 200,
                    price=stock_price,
                    volume=1_000 + minute * 100,
                ),
                _tick(
                    minute,
                    sequence=150 + minute * 200,
                    token="999",
                    symbol="NIFTY",
                    price=Decimal("100") + Decimal(minute) / Decimal("10"),
                    volume=0,
                    average=None,
                ),
            )
        )
    evaluation = OPEN + timedelta(minutes=20, seconds=1)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_ticks(ticks)
        projection = _projection(recorder, stale_after_seconds=120)
        projection.initialize(now=evaluation, allow_rebuild=True)
        view = projection.view(now=evaluation)
        replay = MarketReplayEngine(stale_after_seconds=120).replay(
            ticks, evaluation_at=evaluation
        )
        prior = {
            "1": _prior_five_minute("1", "ABC-EQ", Decimal("98")),
            "999": _prior_five_minute("999", "NIFTY", Decimal("98")),
        }
        projection_result = _scanner_result(
            stock,
            benchmark,
            prior,
            view.candles,
            view.health,
            view.session_references,
            view.opening_range_references,
            recorder,
            evaluation,
        )
        replay_result = _scanner_result(
            stock,
            benchmark,
            prior,
            replay.candles,
            replay.health,
            replay.session_references,
            replay.opening_range_references,
            recorder,
            evaluation,
        )

    assert projection_result.to_dict() == replay_result.to_dict()
    assert projection_result.long_candidates
    assert (
        projection_result.long_candidates[0].qualified
        == replay_result.long_candidates[0].qualified
    )
    assert (
        projection_result.long_candidates[0].qualification_reasons
        == replay_result.long_candidates[0].qualification_reasons
    )


def test_projection_writer_persists_feed_events_in_fifo_epoch_order() -> None:
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        projection = _projection(recorder)
        projection.initialize(now=OPEN, allow_rebuild=True)
        writer = ProjectionMarketDataWriter(recorder, projection)
        writer.record_feed_event(_event(FeedEventType.CONNECTED, 0))
        writer.record_ticks((_tick(0, second=1),))
        writer.record_feed_event(_event(FeedEventType.CONNECTED, 1))
        writer.record_ticks(
            (
                _tick(1, second=1),
                _tick(1, second=2),
                _tick(1, second=3),
            )
        )
        view = projection.view(now=OPEN + timedelta(minutes=1, seconds=4))

    assert view.checkpoint.last_feed_event_id == 2
    assert view.health[0].feed_epoch == 1
    assert view.health[0].trusted


def test_projection_failure_leaves_raw_tick_for_restart_catch_up() -> None:
    class FailingProjection:
        def process_tick_records(self, _records) -> None:
            raise RuntimeError("projection unavailable")

    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        writer = ProjectionMarketDataWriter(recorder, FailingProjection())
        with pytest.raises(ProjectionWriterError, match="suffix catch-up"):
            writer.record_ticks((_tick(0, second=1),))

        assert recorder.count_ticks() == 1
        assert recorder.load_projection_checkpoint(SESSION) is None


def test_read_only_projection_view_performs_no_projection_write(monkeypatch) -> None:
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_tick(_tick(0, second=1))
        projection = _projection(recorder)
        projection.initialize(now=OPEN + timedelta(seconds=2), allow_rebuild=True)
        before = recorder.load_projection_checkpoint(SESSION)
        assert before is not None

        def unexpected_write(**_kwargs) -> None:
            raise AssertionError("read-only projection load attempted a write")

        monkeypatch.setattr(recorder, "save_projection_state", unexpected_write)
        view = recorder.load_projection_view(
            SESSION, now=OPEN + timedelta(seconds=3)
        )
        after = recorder.load_projection_checkpoint(SESSION)

    assert view.checkpoint == before
    assert after == before
    assert after.updated_at == before.updated_at
    assert after.last_tick_id == before.last_tick_id
    assert after.engine_state == before.engine_state


def test_read_only_projection_view_fails_when_recorder_catch_up_is_pending() -> None:
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_tick(_tick(0, second=1))
        projection = _projection(recorder)
        projection.initialize(now=OPEN + timedelta(seconds=2), allow_rebuild=True)
        recorder.record_tick(_tick(0, second=2))

        with pytest.raises(
            ProjectionCatchUpPendingError, match="projection_catch_up_pending"
        ):
            recorder.load_projection_view(
                SESSION, now=OPEN + timedelta(seconds=3)
            )


def test_latest_projection_read_accepts_checkpoint_after_command_start() -> None:
    checkpoint_time = OPEN + timedelta(seconds=10)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_tick(_tick(0, second=9))
        projection = _projection(recorder)
        projection.initialize(now=checkpoint_time, allow_rebuild=True)

        latest = recorder.load_projection_view(SESSION)
        with pytest.raises(ProjectionCutoffBehindCheckpointError):
            recorder.load_projection_view(
                SESSION, now=OPEN + timedelta(seconds=5)
            )

    assert latest.checkpoint.updated_at == checkpoint_time


def test_stale_projection_writer_cas_cannot_overwrite_newer_recorder(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'projection-race.db'}"
    with (
        SQLiteMarketRecorder(database_url) as first_recorder,
        SQLiteMarketRecorder(database_url) as second_recorder,
    ):
        first_recorder.record_tick(_tick(0, second=1))
        stale_projection = _projection(first_recorder)
        stale_projection.initialize(
            now=OPEN + timedelta(seconds=2), allow_rebuild=True
        )

        live_projection = _projection(second_recorder)
        live_projection.initialize(
            now=OPEN + timedelta(seconds=3), allow_rebuild=False
        )
        writer = ProjectionMarketDataWriter(second_recorder, live_projection)
        writer.record_ticks((_tick(0, second=4),))
        advanced = second_recorder.load_projection_checkpoint(SESSION)
        assert advanced is not None and advanced.last_tick_id == 2

        with pytest.raises(ProjectionConcurrentAdvanceError):
            stale_projection.view(now=OPEN + timedelta(seconds=5))

        preserved = second_recorder.load_projection_checkpoint(SESSION)

    assert preserved == advanced


def test_projection_snapshot_remains_commit_consistent_during_live_advances(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'projection-snapshot.db'}"
    with (
        SQLiteMarketRecorder(database_url) as writer_recorder,
        SQLiteMarketRecorder(database_url) as reader_recorder,
    ):
        writer_recorder.record_tick(_tick(0, second=1, sequence=1))
        projection = _projection(writer_recorder)
        projection.initialize(
            now=OPEN + timedelta(seconds=2), allow_rebuild=True
        )
        writer = ProjectionMarketDataWriter(writer_recorder, projection)

        def advance() -> None:
            for index in range(2, 150):
                writer.record_ticks(
                    (
                        _tick(
                            index // 60,
                            second=index % 60,
                            sequence=index,
                        ),
                    )
                )

        worker = threading.Thread(target=advance)
        worker.start()
        observed = 0
        while worker.is_alive():
            try:
                view = reader_recorder.load_projection_view(
                    SESSION, now=OPEN + timedelta(minutes=3)
                )
            except ProjectionCatchUpPendingError:
                continue
            observed += 1
            health_state = view.checkpoint.engine_state["health"]
            assert isinstance(health_state, dict)
            states = health_state["states"]
            assert isinstance(states, list)
            assert view.health[0].sequence_number == states[0]["sequence"]
            assert (
                view.session_references[0].observed_at
                == view.health[0].latest_exchange_timestamp
            )
        worker.join()
        final = reader_recorder.load_projection_view(
            SESSION, now=OPEN + timedelta(minutes=3)
        )

    assert observed > 0
    assert final.checkpoint.last_tick_id == 149
    assert final.health[0].sequence_number == 149


def test_projection_freshness_gate_is_fail_closed_only_during_live_session() -> None:
    live_now = datetime(2026, 8, 10, 10, 0, tzinfo=IST)
    after_close = datetime(2026, 8, 10, 16, 0, tzinfo=IST)

    assert cli._projection_freshness_reason(
        now=live_now,
        projection_age_seconds=5.1,
        last_received_at=live_now - timedelta(seconds=5.1),
        stale_after_seconds=5,
    ) == "projection_stale:5.1>5"
    assert (
        cli._projection_freshness_reason(
            now=after_close,
            projection_age_seconds=3_600,
            last_received_at=after_close - timedelta(hours=1),
            stale_after_seconds=5,
        )
        is None
    )
    assert cli._projection_freshness_reason(
        now=live_now,
        projection_age_seconds=0,
        last_received_at=live_now + timedelta(seconds=1),
        stale_after_seconds=5,
    ) == "projection_timestamp_in_future"
    assert cli._projection_freshness_reason(
        now=live_now,
        projection_age_seconds=0,
        last_received_at=live_now,
        checkpoint_updated_at=live_now + timedelta(seconds=1),
        stale_after_seconds=5,
    ) == "projection_timestamp_in_future"
    assert cli._projection_age_seconds(
        now=live_now,
        last_received_at=live_now - timedelta(seconds=3),
    ) == 3
    payload = cli._projection_not_ready_payload(
        settings=Settings(_env_file=None),
        nifty_count=1,
        universe=SimpleNamespace(
            instruments=(),
            expected_eligibility_exclusions=(),
            mapping_failures=(),
        ),
        reason="projection_stale:5.1>5",
        load_seconds=0.01,
        projection_age_seconds=5.1,
    )
    assert payload["readiness"] == "NOT_READY"
    assert payload["full_replay_used"] is False


def test_normal_scan_uses_projection_telemetry_and_never_calls_replay(
    monkeypatch, capsys
) -> None:
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:")
    command_started_at = OPEN + timedelta(minutes=1)
    checkpoint_updated_at = command_started_at + timedelta(seconds=2)
    freshness_sampled_at = checkpoint_updated_at + timedelta(seconds=1)
    clock_values = iter((command_started_at, freshness_sampled_at))
    universe = SimpleNamespace(
        instruments=(),
        expected_eligibility_exclusions=(),
        mapping_failures=(),
    )
    instrument_master = SimpleNamespace(find_trading_symbol=lambda *_args: None)
    nifty = SimpleNamespace(constituents=("ABC",))

    class Cache:
        def __init__(self, value) -> None:
            self.value = value

        def load(self, *, now):
            return self.value

    class Recorder:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def load_candles(self, **_kwargs):
            return []

        def load_projection_view(self, _session_date):
            return view

    checkpoint = SimpleNamespace(
        last_tick_id=77,
        last_received_at=checkpoint_updated_at,
        updated_at=checkpoint_updated_at,
    )
    view = SimpleNamespace(
        checkpoint=checkpoint,
        projection_age_seconds=999.0,
        candles=(),
        health=(),
        session_references=(),
        opening_range_references=(),
    )
    captured: dict[str, object] = {}

    class Result:
        readiness = SimpleNamespace(value="READY")

        def to_dict(self):
            return {
                "readiness": "READY",
                "market_regime": "RANGE",
                "analysis_only": True,
                "long_candidates": [],
                "short_candidates": [],
                "qualified_long_signals": [],
                "qualified_short_signals": [],
                "qualified_long_count": 0,
                "qualified_short_count": 0,
                "diagnostics": {},
                **captured,
            }

    class Scanner:
        def scan(self, **kwargs):
            captured.update(
                {
                    "raw_ticks_replayed": kwargs["raw_ticks_replayed"],
                    "state_source": kwargs["state_source"],
                    "projection_age_seconds": kwargs[
                        "projection_age_seconds"
                    ],
                    "incremental_ticks_processed": kwargs[
                        "incremental_ticks_processed"
                    ],
                    "full_replay_used": kwargs["full_replay_used"],
                    "projection_last_tick_id": kwargs[
                        "projection_last_tick_id"
                    ],
                    "scanner_evaluation_at": kwargs["now"].isoformat(),
                }
            )
            return Result()

    monkeypatch.setattr(
        cli, "_instrument_cache", lambda _settings: Cache(instrument_master)
    )
    monkeypatch.setattr(cli, "_intraday_cache", lambda _settings: Cache(object()))
    monkeypatch.setattr(cli, "_nifty500_cache", lambda _settings: Cache(nifty))
    monkeypatch.setattr(
        cli, "resolve_nse_equity_universe", lambda *_args, **_kwargs: universe
    )
    monkeypatch.setattr(cli, "_recorder", lambda _settings: Recorder())
    monkeypatch.setattr(cli, "_ist_now", lambda: next(clock_values))
    monkeypatch.setattr(
        cli,
        "_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("normal scan must not construct a mutable projection")
        ),
    )
    monkeypatch.setattr(cli, "_market_scanner", lambda _settings: Scanner())
    monkeypatch.setattr(
        cli,
        "_replay_engine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("normal scan must not construct the replay engine")
        ),
    )

    assert cli._scan(settings, json_output=True, limit=10) == 0
    payload = capsys.readouterr().out

    assert '"state_source": "INCREMENTAL_PROJECTION"' in payload
    assert '"raw_ticks_replayed": 0' in payload
    assert '"full_replay_used": false' in payload
    assert '"projection_last_tick_id": 77' in payload
    assert '"projection_age_seconds": 1.0' in payload
    assert checkpoint_updated_at.isoformat() in payload


def test_scan_fails_closed_on_projection_concurrency_conflict(
    monkeypatch, capsys
) -> None:
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:")
    universe = SimpleNamespace(
        instruments=(),
        expected_eligibility_exclusions=(),
        mapping_failures=(),
    )

    class Cache:
        def __init__(self, value) -> None:
            self.value = value

        def load(self, *, now):
            return self.value

    class Recorder:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def load_projection_view(self, _session_date):
            raise ProjectionConcurrentAdvanceError("simulated recorder advance")

    instrument_master = SimpleNamespace(find_trading_symbol=lambda *_args: None)
    monkeypatch.setattr(
        cli, "_instrument_cache", lambda _settings: Cache(instrument_master)
    )
    monkeypatch.setattr(cli, "_intraday_cache", lambda _settings: Cache(object()))
    monkeypatch.setattr(
        cli,
        "_nifty500_cache",
        lambda _settings: Cache(SimpleNamespace(constituents=("ABC",))),
    )
    monkeypatch.setattr(
        cli, "resolve_nse_equity_universe", lambda *_args, **_kwargs: universe
    )
    monkeypatch.setattr(cli, "_recorder", lambda _settings: Recorder())

    assert cli._scan(settings, json_output=True, limit=10) == 2
    payload = capsys.readouterr().out

    assert "ProjectionConcurrentAdvanceError" in payload
    assert '"readiness": "NOT_READY"' in payload
    assert '"full_replay_used": false' in payload


def test_compare_projection_replay_does_not_mutate_checkpoint(
    monkeypatch, tmp_path, capsys
) -> None:
    database_url = f"sqlite:///{tmp_path / 'projection-compare.db'}"
    settings = Settings(_env_file=None, database_url=database_url)
    with SQLiteMarketRecorder(database_url) as recorder:
        recorder.record_tick(_tick(0, second=1))
        projection = _projection(recorder)
        projection.initialize(now=OPEN + timedelta(seconds=2), allow_rebuild=True)
        before = recorder.load_projection_checkpoint(SESSION)
    assert before is not None
    replay_engine = cli._replay_engine(settings)
    replay_cutoffs: list[datetime] = []

    class ReplayEngine:
        def replay_date(self, recorder, session_date, **kwargs):
            replay_cutoffs.append(kwargs["evaluation_at"])
            return replay_engine.replay_date(recorder, session_date, **kwargs)

    class Cache:
        def __init__(self, value) -> None:
            self.value = value

        def load(self, *, now):
            return self.value

    instrument_master = SimpleNamespace(find_trading_symbol=lambda *_args: None)
    universe = SimpleNamespace(
        instruments=(),
        expected_eligibility_exclusions=(),
        mapping_failures=(),
    )
    monkeypatch.setattr(
        cli, "_instrument_cache", lambda _settings: Cache(instrument_master)
    )
    monkeypatch.setattr(cli, "_intraday_cache", lambda _settings: Cache(object()))
    monkeypatch.setattr(
        cli,
        "_nifty500_cache",
        lambda _settings: Cache(SimpleNamespace(constituents=())),
    )
    monkeypatch.setattr(
        cli, "resolve_nse_equity_universe", lambda *_args, **_kwargs: universe
    )
    clock_values = iter(
        (OPEN + timedelta(seconds=1), OPEN + timedelta(seconds=3))
    )
    monkeypatch.setattr(cli, "_ist_now", lambda: next(clock_values))
    monkeypatch.setattr(cli, "_replay_engine", lambda _settings: ReplayEngine())
    monkeypatch.setattr(
        cli,
        "_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("comparison must not construct a mutable projection")
        ),
    )

    assert (
        cli._compare_projection_replay(
            settings, session_date=SESSION, json_output=True
        )
        == 0
    )
    payload = capsys.readouterr().out
    with SQLiteMarketRecorder(database_url) as recorder:
        after = recorder.load_projection_checkpoint(SESSION)

    assert after == before
    assert replay_cutoffs == [before.updated_at]
    assert before.updated_at.isoformat() in payload


def _health_semantics(items) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.exchange,
            item.symbol_token,
            item.feed_epoch,
            item.status,
            item.latest_exchange_timestamp,
            item.latest_received_timestamp,
            item.sequence_number,
            item.duplicate_count,
            item.out_of_order_count,
            item.sequence_jump_count,
        )
        for item in items
    )


def _prior_five_minute(
    token: str, symbol: str, start_price: Decimal
) -> list[Candle]:
    prior_open = OPEN - timedelta(days=3)
    candles: list[Candle] = []
    for index in range(20):
        price = start_price + Decimal(index) / Decimal("10")
        start_at = prior_open + timedelta(minutes=index * 5)
        candles.append(
            Candle(
                exchange=Exchange.NSE_CM,
                symbol_token=token,
                trading_symbol=symbol,
                interval=CandleInterval.FIVE_MINUTES,
                start_at=start_at,
                end_at=start_at + timedelta(minutes=5),
                open_price=price,
                high_price=price + Decimal("1"),
                low_price=price - Decimal("1"),
                close_price=price + Decimal("0.1"),
                volume=1_000,
                tick_count=5,
                completed=True,
                trusted=True,
                provenance=MarketDataProvenance.ANGEL_HISTORICAL,
            )
        )
    return candles


def _scanner_result(
    stock,
    benchmark,
    prior,
    current_candles,
    health,
    references,
    opening_references,
    baseline_store,
    evaluation,
):
    grouped = {
        (Exchange.NSE_CM, token): [
            *items,
            *[
                candle
                for candle in current_candles
                if candle.symbol_token == token
            ],
        ]
        for token, items in prior.items()
    }
    return MarketScanner().scan(
        instruments=(stock,),
        candles_by_instrument=grouped,
        health_by_instrument={
            (item.exchange, item.symbol_token): item for item in health
        },
        benchmark=benchmark,
        baseline_store=_FixedBaseline(),
        now=evaluation,
        session_references={
            (item.exchange, item.symbol_token): item for item in references
        },
        opening_range_references={
            (item.exchange, item.symbol_token): item
            for item in opening_references
        },
    )


class _FixedBaseline:
    def load_rvol_baselines(self, **_kwargs):
        return (1_000,) * 10


def _candle_semantics(items) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.exchange,
            item.symbol_token,
            item.interval,
            item.start_at,
            item.end_at,
            item.open_price,
            item.high_price,
            item.low_price,
            item.close_price,
            item.volume,
            item.tick_count,
            item.trusted,
            item.provenance,
        )
        for item in sorted(
            items,
            key=lambda candle: (
                candle.exchange.value,
                candle.symbol_token,
                candle.start_at,
                candle.interval,
            ),
        )
    )
