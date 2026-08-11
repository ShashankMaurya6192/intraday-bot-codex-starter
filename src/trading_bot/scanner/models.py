"""Explainable analysis-only scanner result models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from trading_bot.marketdata import MarketDataHealthStatus, MarketRegime


class ScannerReadiness(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    NOT_READY = "NOT_READY"


class CandidateBias(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class ScannerWeights:
    vwap: Decimal = Decimal("2")
    ema: Decimal = Decimal("2")
    relative_volume: Decimal = Decimal("2")
    relative_strength: Decimal = Decimal("2")
    opening_range_proximity: Decimal = Decimal("1")
    market_regime: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        for field_name in (
            "vwap",
            "ema",
            "relative_volume",
            "relative_strength",
            "opening_range_proximity",
            "market_regime",
        ):
            value = Decimal(str(getattr(self, field_name)))
            if value < 0 or not value.is_finite():
                raise ValueError("scanner weights must be finite and non-negative")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class ScannerCandidate:
    bias: CandidateBias
    symbol: str
    symbol_token: str
    ltp: Decimal
    data_age_seconds: float
    health: MarketDataHealthStatus
    vwap: Decimal
    vwap_distance_pct: Decimal
    vwap_distance_atr: Decimal
    ema9: Decimal
    ema20: Decimal
    atr14: Decimal
    relative_volume: Decimal
    relative_strength: Decimal
    opening_range_high: Decimal
    opening_range_low: Decimal
    distance_to_breakout_pct: Decimal
    distance_to_breakdown_pct: Decimal
    sector: str | None
    market_regime: MarketRegime
    component_scores: Mapping[str, Decimal] = field(default_factory=dict)
    total_score: Decimal = Decimal(0)
    rejection_reasons: tuple[str, ...] = ()
    qualified: bool = False
    qualification_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "bias": self.bias.value,
            "symbol": self.symbol,
            "symbol_token": self.symbol_token,
            "ltp": str(self.ltp),
            "data_age_seconds": self.data_age_seconds,
            "health": self.health.value,
            "vwap": str(self.vwap),
            "vwap_distance_pct": str(self.vwap_distance_pct),
            "vwap_distance_atr": str(self.vwap_distance_atr),
            "ema9": str(self.ema9),
            "ema20": str(self.ema20),
            "atr14": str(self.atr14),
            "relative_volume": str(self.relative_volume),
            "relative_strength": str(self.relative_strength),
            "opening_range_high": str(self.opening_range_high),
            "opening_range_low": str(self.opening_range_low),
            "distance_to_breakout_pct": str(self.distance_to_breakout_pct),
            "distance_to_breakdown_pct": str(self.distance_to_breakdown_pct),
            "sector": self.sector,
            "market_regime": self.market_regime.value,
            "component_scores": {
                key: str(value) for key, value in self.component_scores.items()
            },
            "total_score": str(self.total_score),
            "rejection_reasons": list(self.rejection_reasons),
            "qualified": self.qualified,
            "qualification_reasons": list(self.qualification_reasons),
        }


@dataclass(frozen=True, slots=True)
class ScannerDiagnostics:
    expected_eligibility_exclusions: tuple[str, ...] = ()
    reference_failures: tuple[str, ...] = ()
    unhealthy_market_data: tuple[str, ...] = ()
    indicator_not_ready: tuple[str, ...] = ()
    benchmark_failures: tuple[str, ...] = ()
    scaling_warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "expected_eligibility_exclusions": list(
                self.expected_eligibility_exclusions
            ),
            "reference_failures": list(self.reference_failures),
            "unhealthy_market_data": list(self.unhealthy_market_data),
            "indicator_not_ready": list(self.indicator_not_ready),
            "benchmark_failures": list(self.benchmark_failures),
            "scaling_warnings": list(self.scaling_warnings),
        }

    @property
    def all_messages(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    *self.expected_eligibility_exclusions,
                    *self.reference_failures,
                    *self.unhealthy_market_data,
                    *self.indicator_not_ready,
                    *self.benchmark_failures,
                    *self.scaling_warnings,
                }
            )
        )


@dataclass(frozen=True, slots=True)
class ScannerResult:
    readiness: ScannerReadiness
    market_regime: MarketRegime
    long_candidates: tuple[ScannerCandidate, ...]
    short_candidates: tuple[ScannerCandidate, ...]
    evaluated_count: int
    rejected_count: int
    unhealthy_count: int
    requested_count: int
    eligible_count: int
    expected_ineligible_count: int
    mapping_failure_count: int
    indicator_not_ready_count: int
    benchmark_failure_count: int
    raw_ticks_replayed: int
    replay_elapsed_seconds: float
    diagnostics: ScannerDiagnostics
    state_source: str = "DIRECT_INPUT"
    projection_age_seconds: float | None = None
    projection_load_seconds: float = 0.0
    incremental_ticks_processed: int = 0
    full_replay_used: bool = False
    projection_last_tick_id: int | None = None

    @property
    def qualified_long_signals(self) -> tuple[ScannerCandidate, ...]:
        return tuple(item for item in self.long_candidates if item.qualified)

    @property
    def qualified_short_signals(self) -> tuple[ScannerCandidate, ...]:
        return tuple(item for item in self.short_candidates if item.qualified)

    def to_dict(self) -> dict[str, object]:
        return {
            "readiness": self.readiness.value,
            "market_regime": self.market_regime.value,
            "long_candidates": [item.to_dict() for item in self.long_candidates],
            "short_candidates": [item.to_dict() for item in self.short_candidates],
            "qualified_long_signals": [
                item.to_dict() for item in self.qualified_long_signals
            ],
            "qualified_short_signals": [
                item.to_dict() for item in self.qualified_short_signals
            ],
            "qualified_long_count": len(self.qualified_long_signals),
            "qualified_short_count": len(self.qualified_short_signals),
            "evaluated_count": self.evaluated_count,
            "rejected_count": self.rejected_count,
            "unhealthy_count": self.unhealthy_count,
            "requested_count": self.requested_count,
            "eligible_count": self.eligible_count,
            "expected_ineligible_count": self.expected_ineligible_count,
            "mapping_failure_count": self.mapping_failure_count,
            "indicator_not_ready_count": self.indicator_not_ready_count,
            "benchmark_failure_count": self.benchmark_failure_count,
            "raw_ticks_replayed": self.raw_ticks_replayed,
            "replay_elapsed_seconds": self.replay_elapsed_seconds,
            "state_source": self.state_source,
            "projection_age_seconds": self.projection_age_seconds,
            "projection_load_seconds": self.projection_load_seconds,
            "incremental_ticks_processed": self.incremental_ticks_processed,
            "full_replay_used": self.full_replay_used,
            "projection_last_tick_id": self.projection_last_tick_id,
            "diagnostics": self.diagnostics.to_dict(),
            "analysis_only": True,
        }


@dataclass(frozen=True, slots=True)
class SignalQualification:
    qualified: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.qualified == bool(self.reasons):
            raise ValueError("qualified must be true exactly when reasons are empty")
