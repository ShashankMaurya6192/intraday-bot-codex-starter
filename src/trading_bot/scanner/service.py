"""Deterministic confluence scanner; produces analysis, never trade instructions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Protocol

from trading_bot.broker import BrokerInstrument
from trading_bot.domain import IST, Exchange
from trading_bot.indicators import (
    atr,
    cumulative_volume,
    ema,
    opening_range,
    relative_strength,
    relative_volume,
    session_vwap,
    vwap_distance,
)
from trading_bot.marketdata import (
    Candle,
    CandleInterval,
    InstrumentHealth,
    MarketRegime,
    classify_market_regime,
)

from .models import (
    CandidateBias,
    ScannerCandidate,
    ScannerDiagnostics,
    ScannerReadiness,
    ScannerResult,
    ScannerWeights,
)


class RelativeVolumeBaselineReader(Protocol):
    def load_rvol_baselines(
        self,
        *,
        exchange: Exchange,
        symbol_token: str,
        before_date: date,
        minute_offset: int,
        lookback_sessions: int,
    ) -> tuple[int, ...]: ...


class MarketScanner:
    def __init__(
        self,
        *,
        weights: ScannerWeights | None = None,
        relative_volume_threshold: Decimal = Decimal("1.5"),
        relative_volume_lookback_sessions: int = 15,
        relative_volume_minimum_sessions: int = 10,
        opening_range_minutes: int = 15,
        breakout_proximity_pct: Decimal = Decimal("0.25"),
        regime_momentum_threshold_pct: Decimal = Decimal("0.05"),
        regime_slope_lookback_candles: int = 3,
    ) -> None:
        if relative_volume_lookback_sessions < relative_volume_minimum_sessions:
            raise ValueError("RVOL lookback cannot be shorter than its minimum")
        if relative_volume_minimum_sessions < 1:
            raise ValueError("RVOL minimum sessions must be positive")
        self._weights = weights or ScannerWeights()
        self._rvol_threshold = Decimal(str(relative_volume_threshold))
        self._rvol_lookback = relative_volume_lookback_sessions
        self._rvol_minimum = relative_volume_minimum_sessions
        self._opening_range_minutes = opening_range_minutes
        self._breakout_proximity_pct = Decimal(str(breakout_proximity_pct))
        self._regime_momentum = Decimal(str(regime_momentum_threshold_pct))
        self._regime_slope = regime_slope_lookback_candles

    def scan(
        self,
        *,
        instruments: Sequence[BrokerInstrument],
        candles_by_instrument: Mapping[tuple[Exchange, str], Sequence[Candle]],
        health_by_instrument: Mapping[tuple[Exchange, str], InstrumentHealth],
        benchmark: BrokerInstrument | None,
        baseline_store: RelativeVolumeBaselineReader,
        now: datetime,
        expected_eligibility_diagnostics: Sequence[str] = (),
        reference_diagnostics: Sequence[str] = (),
        requested_count: int | None = None,
        caches_ready: bool = True,
        raw_ticks_replayed: int = 0,
        replay_elapsed_seconds: float = 0.0,
        scaling_warnings: Sequence[str] = (),
    ) -> ScannerResult:
        now_ist = _aware(now)
        eligibility_diagnostics = list(expected_eligibility_diagnostics)
        reference_failures = list(reference_diagnostics)
        market_data_failures: list[str] = []
        indicator_failures: list[str] = []
        benchmark_failures: list[str] = []
        if not caches_ready:
            reference_failures.append("required_reference_cache_not_ready")
        benchmark_key = (
            (benchmark.exchange, benchmark.symbol_token) if benchmark else None
        )
        benchmark_candles = (
            list(candles_by_instrument.get(benchmark_key, ()))
            if benchmark_key is not None
            else []
        )
        benchmark_health = (
            health_by_instrument.get(benchmark_key)
            if benchmark_key is not None
            else None
        )
        regime = classify_market_regime(
            benchmark_candles,
            benchmark_health,
            momentum_threshold_pct=self._regime_momentum,
            slope_lookback_candles=self._regime_slope,
            session_date=now_ist.date(),
            evaluation_at=now_ist,
        )
        if benchmark is None:
            benchmark_failures.append("authoritative_nifty_benchmark_mapping_missing")
        elif benchmark_health is None:
            benchmark_failures.append("nifty_benchmark_market_data_missing")
        elif not benchmark_health.trusted:
            benchmark_failures.append("nifty_benchmark_market_data_untrusted")
        if regime is MarketRegime.UNKNOWN:
            benchmark_failures.append("nifty_market_regime_unknown")

        long_candidates: list[ScannerCandidate] = []
        short_candidates: list[ScannerCandidate] = []
        rejected = 0
        unhealthy = 0
        benchmark_one_minute = _interval(
            benchmark_candles,
            CandleInterval.ONE_MINUTE,
            session_date=now_ist.date(),
            evaluation_at=now_ist,
        )
        benchmark_has_session_open = _starts_at_session_open(
            benchmark_one_minute, now_ist.date()
        )
        if benchmark is not None and not benchmark_has_session_open:
            benchmark_failures.append("nifty_benchmark_session_open_not_ready")
        benchmark_open = (
            benchmark_one_minute[0].open_price
            if benchmark_has_session_open
            else None
        )
        benchmark_current = (
            benchmark_one_minute[-1].close_price if benchmark_one_minute else None
        )

        for instrument in sorted(instruments, key=lambda item: item.trading_symbol):
            key = (instrument.exchange, instrument.symbol_token)
            health = health_by_instrument.get(key)
            if health is None or not health.trusted:
                rejected += 1
                unhealthy += 1
                market_data_failures.append(
                    f"{instrument.trading_symbol}:market_data_untrusted"
                )
                continue
            candles = list(candles_by_instrument.get(key, ()))
            one_minute = _interval(
                candles,
                CandleInterval.ONE_MINUTE,
                session_date=now_ist.date(),
                evaluation_at=now_ist,
            )
            rolling_five_minute = _interval(
                candles,
                CandleInterval.FIVE_MINUTES,
                evaluation_at=now_ist,
            )
            readiness = self._indicator_values(
                instrument=instrument,
                one_minute=one_minute,
                rolling_five_minute=rolling_five_minute,
                benchmark_open=benchmark_open,
                benchmark_current=benchmark_current,
                baseline_store=baseline_store,
                session_date=now_ist.date(),
            )
            if isinstance(readiness, tuple):
                rejected += 1
                indicator_failures.extend(
                    f"{instrument.trading_symbol}:{reason}" for reason in readiness
                )
                continue
            values = readiness
            if regime is MarketRegime.UNKNOWN:
                rejected += 1
                # The benchmark-level failure is enough; avoid duplicating it once
                # per equity in deterministic output.
                continue
            long_candidates.append(
                self._candidate(CandidateBias.LONG, instrument, health, regime, values)
            )
            short_candidates.append(
                self._candidate(CandidateBias.SHORT, instrument, health, regime, values)
            )

        long_candidates.sort(key=lambda item: (-item.total_score, item.symbol))
        short_candidates.sort(key=lambda item: (-item.total_score, item.symbol))
        readiness_status = ScannerReadiness.READY
        if (
            not caches_ready
            or benchmark is None
            or regime is MarketRegime.UNKNOWN
            or benchmark_failures
            or not instruments
        ):
            readiness_status = ScannerReadiness.NOT_READY
        elif reference_failures or market_data_failures or indicator_failures:
            readiness_status = ScannerReadiness.DEGRADED
        diagnostics = ScannerDiagnostics(
            expected_eligibility_exclusions=tuple(
                sorted(set(eligibility_diagnostics))
            ),
            reference_failures=tuple(sorted(set(reference_failures))),
            unhealthy_market_data=tuple(sorted(set(market_data_failures))),
            indicator_not_ready=tuple(sorted(set(indicator_failures))),
            benchmark_failures=tuple(sorted(set(benchmark_failures))),
            scaling_warnings=tuple(sorted(set(scaling_warnings))),
        )
        requested = requested_count if requested_count is not None else len(instruments)
        return ScannerResult(
            readiness=readiness_status,
            market_regime=regime,
            long_candidates=tuple(long_candidates),
            short_candidates=tuple(short_candidates),
            evaluated_count=len(instruments),
            rejected_count=rejected,
            unhealthy_count=unhealthy,
            requested_count=requested,
            eligible_count=len(instruments),
            expected_ineligible_count=len(set(eligibility_diagnostics)),
            mapping_failure_count=len(set(reference_failures)),
            indicator_not_ready_count=len(set(indicator_failures)),
            benchmark_failure_count=len(set(benchmark_failures)),
            raw_ticks_replayed=raw_ticks_replayed,
            replay_elapsed_seconds=replay_elapsed_seconds,
            diagnostics=diagnostics,
        )

    def _indicator_values(
        self,
        *,
        instrument: BrokerInstrument,
        one_minute: list[Candle],
        rolling_five_minute: list[Candle],
        benchmark_open: Decimal | None,
        benchmark_current: Decimal | None,
        baseline_store: RelativeVolumeBaselineReader,
        session_date: date,
    ) -> dict[str, Decimal] | tuple[str, ...]:
        reasons: list[str] = []
        if not one_minute or not rolling_five_minute:
            return ("completed_candles_missing",)
        if not _starts_at_session_open(one_minute, session_date):
            reasons.append("session_open_not_ready")
        closes = [candle.close_price for candle in rolling_five_minute]
        vwap_result = session_vwap(one_minute)
        ema9_result = ema(closes, 9)
        ema20_result = ema(closes, 20)
        atr_result = atr(rolling_five_minute, 14)
        opening_result = opening_range(one_minute, minutes=self._opening_range_minutes)
        latest = one_minute[-1]
        session_open = datetime.combine(session_date, time(9, 15), tzinfo=IST)
        minute_offset = int((latest.start_at - session_open).total_seconds() // 60)
        baselines = baseline_store.load_rvol_baselines(
            exchange=instrument.exchange,
            symbol_token=instrument.symbol_token,
            before_date=session_date,
            minute_offset=minute_offset,
            lookback_sessions=self._rvol_lookback,
        )
        rvol_result = relative_volume(
            cumulative_volume(one_minute),
            baselines,
            minimum_sessions=self._rvol_minimum,
        )
        if benchmark_open is None or benchmark_current is None:
            strength_result = relative_strength(
                stock_open=Decimal(0),
                stock_current=latest.close_price,
                benchmark_open=Decimal(0),
                benchmark_current=Decimal(0),
            )
        else:
            strength_result = relative_strength(
                stock_open=one_minute[0].open_price,
                stock_current=latest.close_price,
                benchmark_open=benchmark_open,
                benchmark_current=benchmark_current,
            )
        named_results = {
            "vwap_not_ready": vwap_result,
            "ema9_not_ready": ema9_result,
            "ema20_not_ready": ema20_result,
            "atr14_not_ready": atr_result,
            "opening_range_not_ready": opening_result,
            "relative_volume_not_ready": rvol_result,
            "relative_strength_not_ready": strength_result,
        }
        reasons.extend(
            name for name, result in named_results.items() if not result.ready
        )
        if reasons:
            return tuple(reasons)
        vwap_value = _required(vwap_result.value)
        atr_value = _required(atr_result.value)
        distance_result = vwap_distance(
            price=latest.close_price,
            vwap=vwap_value,
            atr_value=atr_value,
        )
        if not distance_result.ready or distance_result.value is None:
            return ("vwap_distance_not_ready",)
        opening_value = opening_result.value
        if opening_value is None:
            return ("opening_range_not_ready",)
        return {
            "ltp": latest.close_price,
            "vwap": vwap_value,
            "vwap_distance_pct": distance_result.value.percentage,
            "vwap_distance_atr": distance_result.value.atr_units,
            "ema9": _required(ema9_result.value),
            "ema20": _required(ema20_result.value),
            "atr14": atr_value,
            "relative_volume": _required(rvol_result.value),
            "relative_strength": _required(strength_result.value),
            "opening_high": opening_value.high,
            "opening_low": opening_value.low,
        }

    def _candidate(
        self,
        bias: CandidateBias,
        instrument: BrokerInstrument,
        health: InstrumentHealth,
        regime: MarketRegime,
        values: dict[str, Decimal],
    ) -> ScannerCandidate:
        ltp = values["ltp"]
        breakout_distance = (values["opening_high"] - ltp) / ltp * Decimal(100)
        breakdown_distance = (ltp - values["opening_low"]) / ltp * Decimal(100)
        if bias is CandidateBias.LONG:
            conditions = {
                "vwap": ltp > values["vwap"],
                "ema": values["ema9"] > values["ema20"],
                "relative_volume": values["relative_volume"] >= self._rvol_threshold,
                "relative_strength": values["relative_strength"] > 0,
                "opening_range_proximity": abs(breakout_distance)
                <= self._breakout_proximity_pct,
                "market_regime": regime is MarketRegime.TREND_UP,
            }
        else:
            conditions = {
                "vwap": ltp < values["vwap"],
                "ema": values["ema9"] < values["ema20"],
                "relative_volume": values["relative_volume"] >= self._rvol_threshold,
                "relative_strength": values["relative_strength"] < 0,
                "opening_range_proximity": abs(breakdown_distance)
                <= self._breakout_proximity_pct,
                "market_regime": regime is MarketRegime.TREND_DOWN,
            }
        component_scores = {
            "vwap": self._weights.vwap if conditions["vwap"] else Decimal(0),
            "ema": self._weights.ema if conditions["ema"] else Decimal(0),
            "relative_volume": (
                self._weights.relative_volume
                if conditions["relative_volume"]
                else Decimal(0)
            ),
            "relative_strength": (
                self._weights.relative_strength
                if conditions["relative_strength"]
                else Decimal(0)
            ),
            "opening_range_proximity": (
                self._weights.opening_range_proximity
                if conditions["opening_range_proximity"]
                else Decimal(0)
            ),
            "market_regime": (
                self._weights.market_regime
                if conditions["market_regime"]
                else Decimal(0)
            ),
        }
        return ScannerCandidate(
            bias=bias,
            symbol=instrument.trading_symbol,
            symbol_token=instrument.symbol_token,
            ltp=ltp,
            data_age_seconds=health.data_age_seconds or 0.0,
            health=health.status,
            vwap=values["vwap"],
            vwap_distance_pct=values["vwap_distance_pct"],
            vwap_distance_atr=values["vwap_distance_atr"],
            ema9=values["ema9"],
            ema20=values["ema20"],
            atr14=values["atr14"],
            relative_volume=values["relative_volume"],
            relative_strength=values["relative_strength"],
            opening_range_high=values["opening_high"],
            opening_range_low=values["opening_low"],
            distance_to_breakout_pct=breakout_distance,
            distance_to_breakdown_pct=breakdown_distance,
            sector=None,
            market_regime=regime,
            component_scores=component_scores,
            total_score=sum(component_scores.values(), Decimal(0)),
        )


def _interval(
    candles: Sequence[Candle],
    interval: CandleInterval,
    *,
    session_date: date | None = None,
    evaluation_at: datetime | None = None,
) -> list[Candle]:
    return sorted(
        (
            candle
            for candle in candles
            if candle.interval is interval and candle.completed and candle.trusted
            and (session_date is None or candle.start_at.date() == session_date)
            and (evaluation_at is None or candle.end_at <= evaluation_at)
        ),
        key=lambda candle: candle.start_at,
    )


def _starts_at_session_open(candles: Sequence[Candle], session_date: date) -> bool:
    expected = datetime.combine(session_date, time(9, 15), tzinfo=IST)
    return bool(candles) and candles[0].start_at == expected


def _required(value: Decimal | None) -> Decimal:
    if value is None:
        raise AssertionError("ready indicator unexpectedly has no value")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(IST)
