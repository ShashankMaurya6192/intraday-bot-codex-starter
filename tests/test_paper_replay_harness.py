from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.backtest import (
    ReplayReferenceSnapshot,
    ReplayReferenceSnapshotError,
)
from trading_bot.backtest.replay import (
    ReplayBacktestStatus,
    ReplayHarnessConfig,
    ReplayPaperStrategyHarness,
    ReplayScenario,
)
from trading_bot.broker import BrokerInstrument
from trading_bot.domain import IST, Exchange, MarketTick, SubscriptionMode
from trading_bot.marketdata import (
    MarketDataHealthStatus,
    MarketRegime,
    MarketReplayEngine,
)
from trading_bot.paper import (
    IndianEquityIntradayCostSchedule,
    PaperExecutionConfig,
    PaperReportStatus,
)
from trading_bot.risk import RiskLimits
from trading_bot.scanner import (
    CandidateBias,
    ScannerCandidate,
    ScannerDiagnostics,
    ScannerReadiness,
    ScannerResult,
)
from trading_bot.storage import SQLiteMarketRecorder
from trading_bot.strategy import PaperIntentPolicyConfig

SESSION = date(2026, 10, 20)


def _equity() -> BrokerInstrument:
    return BrokerInstrument(
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        tick_size=Decimal("0.05"),
        lot_size=1,
        instrument_type="EQ",
    )


def _benchmark() -> BrokerInstrument:
    return BrokerInstrument(
        exchange=Exchange.NSE_CM,
        symbol_token="99926000",
        trading_symbol="NIFTY 50",
        tick_size=Decimal(0),
        lot_size=1,
        instrument_type="AMXIDX",
    )


def _snapshot(*, captured_minute: int = 0) -> ReplayReferenceSnapshot:
    return ReplayReferenceSnapshot(
        session_date=SESSION,
        captured_at=datetime(2026, 10, 20, 9, captured_minute, tzinfo=IST),
        instrument_master_sha256="a" * 64,
        intraday_universe_sha256="b" * 64,
        nifty500_sha256="c" * 64,
        configured_symbols=("SBIN",),
        instruments=(_equity(),),
        benchmark=_benchmark(),
    )


def _tick(at: datetime, *, sequence: int, price: str, volume: int) -> MarketTick:
    return MarketTick(
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        subscription_mode=SubscriptionMode.QUOTE,
        sequence_number=sequence,
        exchange_timestamp=at,
        received_at=at,
        last_traded_price=Decimal(price),
        last_traded_quantity=1_000,
        volume_trade_for_the_day=volume,
    )


class _AlwaysReadyScanner:
    def __init__(self) -> None:
        self.visible_tick_counts: list[int] = []

    def scan(self, **kwargs: object) -> ScannerResult:
        self.visible_tick_counts.append(int(kwargs["raw_ticks_replayed"]))
        candidate = ScannerCandidate(
            bias=CandidateBias.LONG,
            symbol="SBIN-EQ",
            symbol_token="3045",
            ltp=Decimal("101"),
            data_age_seconds=0,
            health=MarketDataHealthStatus.READY,
            vwap=Decimal("100"),
            vwap_distance_pct=Decimal("1"),
            vwap_distance_atr=Decimal("0.5"),
            ema9=Decimal("100.5"),
            ema20=Decimal("100"),
            atr14=Decimal("2"),
            relative_volume=Decimal("2"),
            relative_strength=Decimal("0.5"),
            opening_range_high=Decimal("100"),
            opening_range_low=Decimal("98"),
            distance_to_breakout_pct=Decimal("-1"),
            distance_to_breakdown_pct=Decimal("3"),
            sector=None,
            market_regime=MarketRegime.TREND_UP,
            qualified=True,
        )
        return ScannerResult(
            readiness=ScannerReadiness.READY,
            market_regime=MarketRegime.TREND_UP,
            long_candidates=(candidate,),
            short_candidates=(),
            evaluated_count=1,
            rejected_count=0,
            unhealthy_count=0,
            requested_count=1,
            eligible_count=1,
            expected_ineligible_count=0,
            mapping_failure_count=0,
            indicator_not_ready_count=0,
            benchmark_failure_count=0,
            raw_ticks_replayed=0,
            replay_elapsed_seconds=0,
            diagnostics=ScannerDiagnostics(),
        )


def _risk_limits() -> RiskLimits:
    return RiskLimits(
        risk_per_trade_pct=Decimal("0.003"),
        max_daily_loss_pct=Decimal("0.009"),
        max_open_positions=2,
        max_trades_per_day=4,
        max_consecutive_losses=3,
        max_capital_utilization_pct=Decimal(1),
    )


def test_replay_reference_snapshot_is_immutable_but_restart_idempotent(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'snapshot.db').as_posix()}"
    with SQLiteMarketRecorder(database_url) as recorder:
        first = recorder.save_replay_reference_snapshot(_snapshot())
        restarted = recorder.save_replay_reference_snapshot(
            _snapshot(captured_minute=1)
        )

        assert restarted == first
        assert recorder.load_replay_reference_snapshot(SESSION) == first
        with pytest.raises(ReplayReferenceSnapshotError, match="conflicts"):
            recorder.save_replay_reference_snapshot(
                replace(_snapshot(), nifty500_sha256="d" * 64)
            )


def test_replay_reference_snapshot_checksum_corruption_fails_closed(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'corrupt.db').as_posix()}"
    recorder = SQLiteMarketRecorder(database_url)
    recorder.save_replay_reference_snapshot(_snapshot())
    recorder._connection.execute(  # noqa: SLF001 - deliberate corruption test
        "UPDATE replay_reference_snapshots SET payload_json = '{}'"
    )
    recorder._connection.commit()  # noqa: SLF001 - deliberate corruption test

    with pytest.raises(ReplayReferenceSnapshotError, match="checksum"):
        recorder.load_replay_reference_snapshot(SESSION)
    recorder.close()


def test_replay_harness_is_causal_isolated_and_runs_sensitivity_scenarios(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'replay.db').as_posix()}"
    recorder = SQLiteMarketRecorder(database_url)
    recorder.save_replay_reference_snapshot(_snapshot())
    start = datetime(2026, 10, 20, 9, 30, tzinfo=IST)
    ticks = (
        _tick(start, sequence=1, price="101", volume=100),
        _tick(
            start + timedelta(minutes=4, seconds=59),
            sequence=2,
            price="101",
            volume=1_100,
        ),
        _tick(
            start + timedelta(minutes=5),
            sequence=3,
            price="101",
            volume=1_200,
        ),
        _tick(
            start + timedelta(minutes=5, seconds=1),
            sequence=4,
            price="101",
            volume=1_300,
        ),
        _tick(
            datetime(2026, 10, 20, 10, 0, tzinfo=IST),
            sequence=5,
            price="97",
            volume=10_000,
        ),
        _tick(
            datetime(2026, 10, 20, 10, 0, 1, tzinfo=IST),
            sequence=6,
            price="95",
            volume=10_100,
        ),
    )
    recorder.record_ticks(ticks)
    scanner = _AlwaysReadyScanner()
    harness = ReplayPaperStrategyHarness(
        replay_engine=MarketReplayEngine(stale_after_seconds=5),
        scanner=scanner,
        risk_limits=_risk_limits(),
        execution_config=PaperExecutionConfig(
            slippage_bps=Decimal(0),
            tick_participation_pct=Decimal(1),
            first_partial_pct=Decimal(0),
            second_partial_pct=Decimal(0),
            stale_after_seconds=5,
        ),
        intent_config=PaperIntentPolicyConfig(stale_after_seconds=5),
        cost_schedule=IndianEquityIntradayCostSchedule(),
        config=ReplayHarnessConfig(
            starting_equity=Decimal("100000"),
            maximum_ticks=100,
            maximum_instruments=2,
            liquidity_participation_pct=Decimal("0.10"),
        ),
        entry_start=datetime.strptime("09:30", "%H:%M").time(),
        last_entry=datetime.strptime("14:45", "%H:%M").time(),
    )

    result = harness.run(
        recorder,
        snapshot=_snapshot(),
        session_date=SESSION,
        scenarios=(
            ReplayScenario("base", Decimal(0)),
            ReplayScenario("stress", Decimal("10"), Decimal(2)),
        ),
    )

    assert result.status is ReplayBacktestStatus.COMPLETE
    assert result.raw_ticks == 6
    assert result.unique_policy_intents == 1
    assert scanner.visible_tick_counts[0] == 1
    assert scanner.visible_tick_counts[1] == 3
    assert max(scanner.visible_tick_counts) == 6
    assert len(result.scenarios) == 2
    assert all(
        scenario.performance.status is PaperReportStatus.COMPLETE
        and scenario.performance.closed_trades == 1
        and scenario.recorded_hard_stop_exits == 1
        and scenario.adverse_gap_exits == 1
        and scenario.maximum_adverse_gap_per_share > 0
        for scenario in result.scenarios
    )
    assert (
        result.scenarios[1].performance.estimated_net_realized_pnl
        < result.scenarios[0].performance.estimated_net_realized_pnl
    )
    assert result.to_dict()["source_market_data_mutated"] is False
    assert recorder.count_ticks() == 6
    recorder.close()
