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
        }


@dataclass(frozen=True, slots=True)
class ScannerResult:
    readiness: ScannerReadiness
    market_regime: MarketRegime
    long_candidates: tuple[ScannerCandidate, ...]
    short_candidates: tuple[ScannerCandidate, ...]
    evaluated_count: int
    rejected_count: int
    unhealthy_count: int
    diagnostics: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "readiness": self.readiness.value,
            "market_regime": self.market_regime.value,
            "long_candidates": [item.to_dict() for item in self.long_candidates],
            "short_candidates": [item.to_dict() for item in self.short_candidates],
            "evaluated_count": self.evaluated_count,
            "rejected_count": self.rejected_count,
            "unhealthy_count": self.unhealthy_count,
            "diagnostics": list(self.diagnostics),
            "analysis_only": True,
        }
