"""Pure, replay-safe market indicators with explicit warm-up states."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from trading_bot.marketdata.models import Candle

T = TypeVar("T")
_HUNDRED = Decimal("100")
_NSE_SESSION_OPEN = time(9, 15)


class IndicatorStatus(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"


@dataclass(frozen=True, slots=True)
class IndicatorResult(Generic[T]):
    status: IndicatorStatus
    value: T | None
    reason: str | None = None

    @property
    def ready(self) -> bool:
        return self.status is IndicatorStatus.READY and self.value is not None

    @classmethod
    def ready_value(cls, value: T) -> IndicatorResult[T]:
        return cls(IndicatorStatus.READY, value)

    @classmethod
    def not_ready(cls, reason: str) -> IndicatorResult[T]:
        return cls(IndicatorStatus.NOT_READY, None, reason)


@dataclass(frozen=True, slots=True)
class OpeningRange:
    high: Decimal
    low: Decimal


@dataclass(frozen=True, slots=True)
class VwapDistance:
    percentage: Decimal
    atr_units: Decimal


def session_vwap(
    candles: list[Candle] | tuple[Candle, ...],
) -> IndicatorResult[Decimal]:
    """Volume-weighted typical price: ``(high + low + close) / 3``.

    Only completed, trusted candles participate. Zero-volume histories are not ready.
    """
    usable = _usable(candles)
    total_volume = sum(candle.volume for candle in usable)
    if total_volume <= 0:
        return IndicatorResult.not_ready("no_completed_trusted_volume")
    weighted = sum(
        ((candle.high_price + candle.low_price + candle.close_price) / Decimal(3))
        * candle.volume
        for candle in usable
    )
    return IndicatorResult.ready_value(weighted / Decimal(total_volume))


def ema(
    values: list[Decimal] | tuple[Decimal, ...], period: int
) -> IndicatorResult[Decimal]:
    """EMA seeded by the simple mean of the first ``period`` observations."""
    if period < 1:
        raise ValueError("EMA period must be positive")
    if len(values) < period:
        return IndicatorResult.not_ready(f"requires_{period}_values")
    normalized = [Decimal(str(value)) for value in values]
    current = sum(normalized[:period], Decimal(0)) / Decimal(period)
    multiplier = Decimal(2) / Decimal(period + 1)
    for value in normalized[period:]:
        current = (value - current) * multiplier + current
    return IndicatorResult.ready_value(current)


def atr(
    candles: list[Candle] | tuple[Candle, ...], period: int = 14
) -> IndicatorResult[Decimal]:
    """Simple average of ``period`` true ranges; requires a prior close."""
    if period < 1:
        raise ValueError("ATR period must be positive")
    usable = _usable(candles)
    if len(usable) < period + 1:
        return IndicatorResult.not_ready(f"requires_{period + 1}_candles")
    selected = usable[-(period + 1) :]
    true_ranges: list[Decimal] = []
    for previous, current in zip(selected[:-1], selected[1:], strict=True):
        true_ranges.append(
            max(
                current.high_price - current.low_price,
                abs(current.high_price - previous.close_price),
                abs(current.low_price - previous.close_price),
            )
        )
    return IndicatorResult.ready_value(sum(true_ranges, Decimal(0)) / Decimal(period))


def relative_volume(
    current_cumulative_volume: int,
    baseline_cumulative_volumes: tuple[int, ...] | list[int],
    *,
    minimum_sessions: int,
) -> IndicatorResult[Decimal]:
    """Compare cumulative volume with prior sessions at the same session minute."""
    if current_cumulative_volume < 0:
        raise ValueError("current cumulative volume must be non-negative")
    if minimum_sessions < 1:
        raise ValueError("minimum_sessions must be positive")
    if len(baseline_cumulative_volumes) < minimum_sessions:
        return IndicatorResult.not_ready("insufficient_valid_baseline_sessions")
    selected = tuple(int(value) for value in baseline_cumulative_volumes)
    if any(value < 0 for value in selected):
        raise ValueError("baseline volumes must be non-negative")
    average = Decimal(sum(selected)) / Decimal(len(selected))
    if average <= 0:
        return IndicatorResult.not_ready("baseline_average_is_zero")
    return IndicatorResult.ready_value(Decimal(current_cumulative_volume) / average)


def relative_strength(
    *,
    stock_open: Decimal,
    stock_current: Decimal,
    benchmark_open: Decimal,
    benchmark_current: Decimal,
) -> IndicatorResult[Decimal]:
    """Stock session return minus benchmark session return, in percentage points."""
    values = tuple(
        Decimal(str(value))
        for value in (stock_open, stock_current, benchmark_open, benchmark_current)
    )
    if any(value <= 0 for value in values):
        return IndicatorResult.not_ready("open_and_current_prices_must_be_positive")
    stock_return = (values[1] / values[0] - 1) * _HUNDRED
    benchmark_return = (values[3] / values[2] - 1) * _HUNDRED
    return IndicatorResult.ready_value(stock_return - benchmark_return)


def vwap_distance(
    *, price: Decimal, vwap: Decimal, atr_value: Decimal
) -> IndicatorResult[VwapDistance]:
    price = Decimal(str(price))
    vwap = Decimal(str(vwap))
    atr_value = Decimal(str(atr_value))
    if price <= 0 or vwap <= 0 or atr_value <= 0:
        return IndicatorResult.not_ready("price_vwap_and_atr_must_be_positive")
    difference = price - vwap
    return IndicatorResult.ready_value(
        VwapDistance(
            percentage=difference / vwap * _HUNDRED,
            atr_units=difference / atr_value,
        )
    )


def opening_range(
    candles: list[Candle] | tuple[Candle, ...],
    *,
    minutes: int = 15,
) -> IndicatorResult[OpeningRange]:
    if minutes < 1:
        raise ValueError("opening-range minutes must be positive")
    usable = _usable(candles)
    if not usable:
        return IndicatorResult.not_ready("opening_range_has_no_candles")
    session_date = usable[0].start_at.date()
    range_start = usable[0].start_at.replace(
        hour=_NSE_SESSION_OPEN.hour,
        minute=_NSE_SESSION_OPEN.minute,
        second=0,
        microsecond=0,
    )
    range_end = range_start + timedelta(minutes=minutes)
    selected = sorted(
        (
            candle
            for candle in usable
            if candle.start_at >= range_start and candle.end_at <= range_end
        ),
        key=lambda candle: candle.start_at,
    )
    covered_minutes = sum(int(candle.interval) for candle in selected)
    continuous = all(
        current.end_at == following.start_at
        for current, following in zip(selected[:-1], selected[1:], strict=True)
    )
    if (
        not selected
        or selected[0].start_at.date() != session_date
        or selected[0].start_at != range_start
        or selected[-1].end_at != range_end
        or covered_minutes != minutes
        or not continuous
    ):
        return IndicatorResult.not_ready("opening_range_is_incomplete")
    return IndicatorResult.ready_value(
        OpeningRange(
            high=max(candle.high_price for candle in selected),
            low=min(candle.low_price for candle in selected),
        )
    )


def cumulative_volume(candles: list[Candle] | tuple[Candle, ...]) -> int:
    return sum(candle.volume for candle in _usable(candles))


def _usable(candles: list[Candle] | tuple[Candle, ...]) -> list[Candle]:
    return [candle for candle in candles if candle.completed and candle.trusted]
