"""Causal, read-only market replay into isolated in-memory PAPER scenarios."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Protocol

from trading_bot.domain import IST, Exchange, FeedEvent, FeedEventType, MarketTick
from trading_bot.marketdata import (
    Candle,
    CandleInterval,
    MarketDataHealthMonitor,
    MarketReplayEngine,
)
from trading_bot.paper import (
    IndianEquityIntradayCostModel,
    IndianEquityIntradayCostSchedule,
    PaperExecutionConfig,
    PaperExecutionEngine,
    PaperOrderPurpose,
    PaperPerformanceAnalyzer,
    PaperPerformanceReport,
    PaperPositionStatus,
    PaperReportStatus,
)
from trading_bot.risk import RiskLimits, TradeSide
from trading_bot.scanner import ScannerReadiness, ScannerResult
from trading_bot.storage.paper import SQLitePaperStore
from trading_bot.strategy import (
    PaperIntentPolicyConfig,
    RelativeStrengthOpeningBreakoutV1IntentPolicy,
)

from .reference import FrozenIntradayAllowedUniverse, ReplayReferenceSnapshot


class ReplayBacktestStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class ReplayBacktestInputError(RuntimeError):
    """Recorded inputs cannot support a trustworthy strategy replay."""


class ReplayMarketStore(Protocol):
    def count_ticks(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        received_at_or_before: datetime | None = None,
    ) -> int: ...

    def load_ticks(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        received_at_or_before: datetime | None = None,
        received_order: bool = False,
    ) -> list[MarketTick]: ...

    def load_feed_events(self, *, limit: int | None = None) -> list[FeedEvent]: ...

    def load_candles(
        self,
        *,
        exchange: Exchange | None = None,
        symbol_token: str | None = None,
        interval: CandleInterval | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> list[Candle]: ...


class ReplayScanner(Protocol):
    def scan(self, **kwargs: object) -> ScannerResult: ...


@dataclass(frozen=True, slots=True)
class ReplayScenario:
    name: str
    slippage_bps: Decimal
    cost_multiplier: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        name = self.name.strip().upper()
        slippage = Decimal(str(self.slippage_bps))
        multiplier = Decimal(str(self.cost_multiplier))
        if not name:
            raise ValueError("replay scenario name must not be blank")
        if not slippage.is_finite() or not Decimal(0) <= slippage <= Decimal(100):
            raise ValueError("scenario slippage_bps must be in [0, 100]")
        if not multiplier.is_finite() or not Decimal(0) < multiplier <= Decimal(10):
            raise ValueError("scenario cost_multiplier must be in (0, 10]")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "slippage_bps", slippage)
        object.__setattr__(self, "cost_multiplier", multiplier)


@dataclass(frozen=True, slots=True)
class ReplayHarnessConfig:
    starting_equity: Decimal
    maximum_ticks: int = 250_000
    maximum_instruments: int = 20
    historical_lookback_days: int = 30
    liquidity_participation_pct: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        equity = Decimal(str(self.starting_equity))
        participation = Decimal(str(self.liquidity_participation_pct))
        if not equity.is_finite() or equity <= 0:
            raise ValueError("replay starting_equity must be positive and finite")
        if self.maximum_ticks < 1 or self.maximum_instruments < 1:
            raise ValueError("replay safety limits must be positive")
        if self.historical_lookback_days < 1:
            raise ValueError("historical_lookback_days must be positive")
        if not participation.is_finite() or not Decimal(0) < participation <= 1:
            raise ValueError("liquidity_participation_pct must be in (0, 1]")
        object.__setattr__(self, "starting_equity", equity)
        object.__setattr__(self, "liquidity_participation_pct", participation)


@dataclass(frozen=True, slots=True)
class ReplayScenarioResult:
    scenario: ReplayScenario
    performance: PaperPerformanceReport
    accepted_entries: int
    rejected_entries: int
    recorded_hard_stop_exits: int
    adverse_gap_exits: int
    maximum_adverse_gap_per_share: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.scenario.name,
            "slippage_bps": str(self.scenario.slippage_bps),
            "cost_multiplier": str(self.scenario.cost_multiplier),
            "accepted_entries": self.accepted_entries,
            "rejected_entries": self.rejected_entries,
            "recorded_hard_stop_exits": self.recorded_hard_stop_exits,
            "adverse_gap_exits": self.adverse_gap_exits,
            "maximum_adverse_gap_per_share": str(
                self.maximum_adverse_gap_per_share
            ),
            "performance": self.performance.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ReplayBacktestResult:
    status: ReplayBacktestStatus
    session_date: date
    reference_snapshot_sha256: str
    raw_ticks: int
    evaluation_count: int
    ready_evaluations: int
    qualified_candidates: int
    unique_policy_intents: int
    policy_rejections: dict[str, int]
    reference_exclusions: tuple[str, ...]
    scenarios: tuple[ReplayScenarioResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "mode": "PAPER_REPLAY_ONLY",
            "session_date": self.session_date.isoformat(),
            "reference_snapshot_sha256": self.reference_snapshot_sha256,
            "raw_ticks": self.raw_ticks,
            "evaluation_count": self.evaluation_count,
            "ready_evaluations": self.ready_evaluations,
            "qualified_candidates": self.qualified_candidates,
            "unique_policy_intents": self.unique_policy_intents,
            "policy_rejections": dict(sorted(self.policy_rejections.items())),
            "reference_exclusions": list(self.reference_exclusions),
            "scenarios": [item.to_dict() for item in self.scenarios],
            "source_market_data_mutated": False,
            "broker_orders_available": False,
        }


@dataclass(slots=True)
class _ScenarioRuntime:
    scenario: ReplayScenario
    store: SQLitePaperStore
    engine: PaperExecutionEngine


class ReplayPaperStrategyHarness:
    """Replay recorded data causally; all simulated state stays in memory."""

    def __init__(
        self,
        *,
        replay_engine: MarketReplayEngine,
        scanner: ReplayScanner,
        risk_limits: RiskLimits,
        execution_config: PaperExecutionConfig,
        intent_config: PaperIntentPolicyConfig,
        cost_schedule: IndianEquityIntradayCostSchedule,
        config: ReplayHarnessConfig,
        entry_start: time,
        last_entry: time,
    ) -> None:
        self._replay = replay_engine
        self._scanner = scanner
        self._risk_limits = risk_limits
        self._execution_config = execution_config
        self._intent_policy = RelativeStrengthOpeningBreakoutV1IntentPolicy(
            intent_config
        )
        self._cost_schedule = cost_schedule
        self._config = config
        self._entry_start = entry_start
        self._last_entry = last_entry

    def run(
        self,
        store: ReplayMarketStore,
        *,
        snapshot: ReplayReferenceSnapshot,
        session_date: date,
        scenarios: Sequence[ReplayScenario],
    ) -> ReplayBacktestResult:
        if snapshot.session_date != session_date:
            raise ReplayBacktestInputError(
                "reference snapshot does not belong to the requested session"
            )
        if len(snapshot.instruments) > self._config.maximum_instruments:
            raise ReplayBacktestInputError(
                "frozen universe exceeds PAPER_REPLAY_MAX_INSTRUMENTS"
            )
        scenario_items = tuple(scenarios)
        if not scenario_items or len({item.name for item in scenario_items}) != len(
            scenario_items
        ):
            raise ValueError("replay scenarios must be non-empty and uniquely named")
        session_start = datetime.combine(session_date, time.min, tzinfo=IST)
        session_end = session_start + timedelta(days=1)
        tick_count = store.count_ticks(start_at=session_start, end_at=session_end)
        if tick_count == 0:
            raise ReplayBacktestInputError("recorded session contains no ticks")
        if tick_count > self._config.maximum_ticks:
            raise ReplayBacktestInputError(
                "recorded session exceeds PAPER_REPLAY_MAX_TICKS"
            )
        ticks = store.load_ticks(
            start_at=session_start,
            end_at=session_end,
            received_order=True,
        )
        events = tuple(
            event
            for event in store.load_feed_events()
            if event.occurred_at.date() == session_date
        )
        connected_starts = tuple(
            sorted(
                event.occurred_at
                for event in events
                if event.event_type is FeedEventType.CONNECTED
            )
        )[1:]
        historical_start = datetime.combine(
            session_date - timedelta(days=self._config.historical_lookback_days),
            time.min,
            tzinfo=IST,
        )
        prior_candles = store.load_candles(
            start_at=historical_start,
            end_at=session_start,
        )
        runtimes = self._scenario_runtimes(session_date, scenario_items)
        monitor = MarketDataHealthMonitor(
            stale_after_seconds=self._execution_config.stale_after_seconds
        )
        timeline = _timeline(ticks, events)
        timeline_index = 0
        connected_seen = 0
        evaluations = 0
        ready_evaluations = 0
        qualified_candidates = 0
        unique_intents: set[str] = set()
        policy_rejections: Counter[str] = Counter()
        frozen_eligibility = FrozenIntradayAllowedUniverse(snapshot.instruments)
        instruments_by_token = {
            (item.exchange, item.symbol_token): item for item in snapshot.instruments
        }

        try:
            for evaluation_at in _evaluation_times(
                session_date,
                entry_start=self._entry_start,
                last_entry=self._last_entry,
            ):
                timeline_index, connected_seen = self._process_until(
                    timeline,
                    timeline_index,
                    connected_seen,
                    until=evaluation_at,
                    monitor=monitor,
                    runtimes=runtimes,
                )
                evaluations += 1
                replay = self._replay.replay(
                    ticks,
                    evaluation_at=evaluation_at,
                    feed_epoch_starts=connected_starts,
                )
                candles_by = _candles_by_instrument(
                    (*prior_candles, *replay.candles)
                )
                result = self._scanner.scan(
                    instruments=snapshot.instruments,
                    candles_by_instrument=candles_by,
                    health_by_instrument={
                        (item.exchange, item.symbol_token): item
                        for item in replay.health
                    },
                    benchmark=snapshot.benchmark,
                    baseline_store=store,
                    now=evaluation_at,
                    expected_eligibility_diagnostics=(),
                    reference_diagnostics=(),
                    requested_count=(
                        len(snapshot.configured_symbols)
                        if snapshot.configured_symbols
                        else len(snapshot.instruments)
                    ),
                    raw_ticks_replayed=replay.raw_ticks_replayed,
                    session_references={
                        (item.exchange, item.symbol_token): item
                        for item in replay.session_references
                    },
                    opening_range_references={
                        (item.exchange, item.symbol_token): item
                        for item in replay.opening_range_references
                    },
                    state_source="PAPER_REPLAY_ONLY",
                    full_replay_used=True,
                )
                if result.readiness is not ScannerReadiness.READY:
                    policy_rejections["scanner_not_ready"] += 1
                    continue
                ready_evaluations += 1
                candidates = sorted(
                    (
                        *result.qualified_long_signals,
                        *result.qualified_short_signals,
                    ),
                    key=lambda item: (-item.total_score, item.symbol, item.bias.value),
                )
                qualified_candidates += len(candidates)
                for candidate in candidates:
                    instrument = instruments_by_token.get(
                        (Exchange.NSE_CM, candidate.symbol_token)
                    )
                    if instrument is None:
                        policy_rejections["instrument_snapshot_mismatch"] += 1
                        continue
                    current_candles = tuple(
                        candle
                        for candle in replay.candles
                        if candle.exchange is instrument.exchange
                        and candle.symbol_token == instrument.symbol_token
                    )
                    liquidity = _liquidity_cap(
                        current_candles,
                        session_date=session_date,
                        evaluation_at=evaluation_at,
                        participation=self._config.liquidity_participation_pct,
                    )
                    decision = self._intent_policy.evaluate(
                        candidate,
                        instrument=instrument,
                        intraday_universe=frozen_eligibility,
                        current_session_five_minute=current_candles,
                        evaluated_at=evaluation_at,
                        max_liquidity_quantity=liquidity,
                    )
                    if not decision.accepted or decision.intent is None:
                        for reason in decision.rejection_reasons:
                            policy_rejections[reason.value] += 1
                        continue
                    unique_intents.add(decision.intent.client_tag)
                    for runtime in runtimes:
                        runtime.engine.submit_entry(decision.intent)

            self._process_until(
                timeline,
                timeline_index,
                connected_seen,
                until=None,
                monitor=monitor,
                runtimes=runtimes,
            )
            scenario_results = tuple(
                self._scenario_result(runtime, session_date) for runtime in runtimes
            )
        finally:
            for runtime in runtimes:
                runtime.store.close()

        status = (
            ReplayBacktestStatus.COMPLETE
            if all(
                item.performance.status is PaperReportStatus.COMPLETE
                for item in scenario_results
            )
            else ReplayBacktestStatus.INCOMPLETE
        )
        return ReplayBacktestResult(
            status=status,
            session_date=session_date,
            reference_snapshot_sha256=snapshot.payload_sha256,
            raw_ticks=tick_count,
            evaluation_count=evaluations,
            ready_evaluations=ready_evaluations,
            qualified_candidates=qualified_candidates,
            unique_policy_intents=len(unique_intents),
            policy_rejections=dict(policy_rejections),
            reference_exclusions=snapshot.exclusions,
            scenarios=scenario_results,
        )

    def _scenario_runtimes(
        self, session_date: date, scenarios: tuple[ReplayScenario, ...]
    ) -> tuple[_ScenarioRuntime, ...]:
        runtimes: list[_ScenarioRuntime] = []
        initialized_at = datetime.combine(session_date, time(9, 15), tzinfo=IST)
        for scenario in scenarios:
            paper_store = SQLitePaperStore("sqlite:///:memory:")
            engine = PaperExecutionEngine(
                paper_store,
                self._risk_limits,
                config=replace(
                    self._execution_config,
                    slippage_bps=scenario.slippage_bps,
                ),
                entry_start=self._entry_start,
                last_entry=self._last_entry,
            )
            engine.initialize_day(
                session_date,
                starting_equity=self._config.starting_equity,
                now=initialized_at,
            )
            runtimes.append(_ScenarioRuntime(scenario, paper_store, engine))
        return tuple(runtimes)

    def _process_until(
        self,
        timeline: tuple[tuple[datetime, int, object], ...],
        index: int,
        connected_seen: int,
        *,
        until: datetime | None,
        monitor: MarketDataHealthMonitor,
        runtimes: tuple[_ScenarioRuntime, ...],
    ) -> tuple[int, int]:
        while index < len(timeline):
            occurred_at, _, item = timeline[index]
            if until is not None and occurred_at > until:
                break
            index += 1
            if isinstance(item, FeedEvent):
                if item.event_type is FeedEventType.CONNECTED:
                    connected_seen += 1
                    if connected_seen > 1:
                        monitor.begin_feed_epoch(at=item.occurred_at)
                elif item.event_type in {
                    FeedEventType.DISCONNECTED,
                    FeedEventType.RECONNECTING,
                    FeedEventType.ERROR,
                }:
                    for runtime in runtimes:
                        if _has_exposure(runtime.store, item.occurred_at.date()):
                            runtime.engine.mark_state_uncertain(
                                item.occurred_at.date(),
                                now=item.occurred_at,
                                reason=f"recorded_feed_{item.event_type.value.lower()}",
                            )
                continue
            if not isinstance(item, MarketTick):
                raise AssertionError("unknown replay timeline item")
            assessment = monitor.observe(item, now=item.received_at)
            if not assessment.accepted:
                continue
            for runtime in runtimes:
                runtime.engine.on_tick(
                    item,
                    health_status=assessment.health.status,
                    observed_at=item.received_at,
                )
        return index, connected_seen

    def _scenario_result(
        self, runtime: _ScenarioRuntime, session_date: date
    ) -> ReplayScenarioResult:
        schedule = _scaled_cost_schedule(
            self._cost_schedule, runtime.scenario.cost_multiplier
        )
        performance = PaperPerformanceAnalyzer(
            runtime.store,
            IndianEquityIntradayCostModel(schedule),
        ).report(from_date=session_date, to_date=session_date)
        orders = runtime.store.list_orders(session_date)
        accepted_entries = sum(
            order.purpose is PaperOrderPurpose.ENTRY
            and order.status.value not in {"REJECTED", "CANCELLED"}
            for order in orders
        )
        rejected_entries = sum(
            order.purpose is PaperOrderPurpose.ENTRY
            and order.status.value in {"REJECTED", "CANCELLED"}
            for order in orders
        )
        hard_stops, adverse_gaps, maximum_gap = _gap_metrics(
            runtime.store, session_date
        )
        return ReplayScenarioResult(
            scenario=runtime.scenario,
            performance=performance,
            accepted_entries=accepted_entries,
            rejected_entries=rejected_entries,
            recorded_hard_stop_exits=hard_stops,
            adverse_gap_exits=adverse_gaps,
            maximum_adverse_gap_per_share=maximum_gap,
        )


def default_replay_scenarios(base_slippage_bps: Decimal) -> tuple[ReplayScenario, ...]:
    base = Decimal(str(base_slippage_bps))
    return (
        ReplayScenario("ZERO_SLIPPAGE", Decimal(0)),
        ReplayScenario("BASE", base),
        ReplayScenario("DOUBLE_SLIPPAGE", min(Decimal(100), base * 2)),
        ReplayScenario("DOUBLE_COST", base, Decimal(2)),
    )


def _evaluation_times(
    session_date: date, *, entry_start: time, last_entry: time
) -> tuple[datetime, ...]:
    current = datetime.combine(session_date, entry_start, tzinfo=IST)
    end = datetime.combine(session_date, last_entry, tzinfo=IST)
    values: list[datetime] = []
    while current <= end:
        values.append(current)
        current += timedelta(minutes=5)
    return tuple(values)


def _timeline(
    ticks: Sequence[MarketTick], events: Sequence[FeedEvent]
) -> tuple[tuple[datetime, int, object], ...]:
    combined = [
        (event.occurred_at, 0, event) for event in events
    ] + [
        (tick.received_at, 1, tick) for tick in ticks
    ]
    return tuple(
        sorted(
            combined,
            key=lambda item: (
                item[0],
                item[1],
                (
                    item[2].sequence_number
                    if isinstance(item[2], MarketTick)
                    else item[2].event_type.value
                ),
            ),
        )
    )


def _candles_by_instrument(
    candles: Sequence[Candle],
) -> dict[tuple[Exchange, str], list[Candle]]:
    grouped: dict[tuple[Exchange, str], dict[tuple[int, datetime], Candle]] = {}
    for candle in candles:
        grouped.setdefault((candle.exchange, candle.symbol_token), {})[
            (int(candle.interval), candle.start_at)
        ] = candle
    return {
        key: sorted(values.values(), key=lambda item: (item.start_at, item.interval))
        for key, values in grouped.items()
    }


def _liquidity_cap(
    candles: Sequence[Candle],
    *,
    session_date: date,
    evaluation_at: datetime,
    participation: Decimal,
) -> int | None:
    eligible = sorted(
        (
            candle
            for candle in candles
            if candle.interval is CandleInterval.FIVE_MINUTES
            and candle.start_at.date() == session_date
            and candle.completed
            and candle.trusted
            and candle.end_at <= evaluation_at
        ),
        key=lambda candle: candle.end_at,
    )
    if not eligible:
        return None
    quantity = int(
        (Decimal(eligible[-1].volume) * participation).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    return quantity if quantity > 0 else None


def _has_exposure(store: SQLitePaperStore, session_date: date) -> bool:
    return any(order.active for order in store.list_orders(session_date)) or any(
        position.status is PaperPositionStatus.OPEN
        for position in store.list_positions(session_date)
    )


def _scaled_cost_schedule(
    schedule: IndianEquityIntradayCostSchedule, multiplier: Decimal
) -> IndianEquityIntradayCostSchedule:
    if multiplier == 1:
        return schedule
    return replace(
        schedule,
        brokerage_rate=schedule.brokerage_rate * multiplier,
        brokerage_minimum_per_order=(
            schedule.brokerage_minimum_per_order * multiplier
        ),
        brokerage_maximum_per_order=(
            schedule.brokerage_maximum_per_order * multiplier
        ),
        exchange_transaction_rate=schedule.exchange_transaction_rate * multiplier,
        stt_sell_rate=schedule.stt_sell_rate * multiplier,
        ipft_rate=schedule.ipft_rate * multiplier,
        sebi_turnover_rate=schedule.sebi_turnover_rate * multiplier,
        stamp_duty_buy_rate=schedule.stamp_duty_buy_rate * multiplier,
        schedule_name=f"{schedule.schedule_name}_X{multiplier}",
    )


def _gap_metrics(
    store: SQLitePaperStore, session_date: date
) -> tuple[int, int, Decimal]:
    orders = store.list_orders(session_date)
    fills = store.list_fills(session_date)
    entry_by_instrument = {
        (order.exchange, order.symbol_token): order
        for order in orders
        if order.purpose is PaperOrderPurpose.ENTRY
    }
    hard_stops = 0
    adverse_gaps = 0
    maximum_gap = Decimal(0)
    for order in orders:
        if order.purpose is not PaperOrderPurpose.HARD_STOP:
            continue
        order_fills = tuple(fill for fill in fills if fill.order_id == order.order_id)
        if not order_fills:
            continue
        hard_stops += 1
        entry = entry_by_instrument.get((order.exchange, order.symbol_token))
        if entry is None or entry.protective_stop is None:
            continue
        total_quantity = sum(fill.quantity for fill in order_fills)
        average_fill = sum(
            (fill.price * fill.quantity for fill in order_fills), Decimal(0)
        ) / total_quantity
        gap = (
            max(Decimal(0), entry.protective_stop - average_fill)
            if order.side is TradeSide.LONG
            else max(Decimal(0), average_fill - entry.protective_stop)
        )
        if gap > 0:
            adverse_gaps += 1
            maximum_gap = max(maximum_gap, gap)
    return hard_stops, adverse_gaps, maximum_gap
