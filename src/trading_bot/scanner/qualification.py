"""Strict, deterministic qualification gates for ranked scanner candidates."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from trading_bot.domain import IST
from trading_bot.marketdata import (
    Candle,
    CandleInterval,
    MarketDataHealthStatus,
    MarketRegime,
)

from .models import CandidateBias, ScannerCandidate, SignalQualification


def qualify_candidate(
    candidate: ScannerCandidate,
    *,
    current_session_five_minute: Sequence[Candle],
    session_date: date,
    evaluation_at: datetime,
    opening_range_minutes: int,
    relative_volume_threshold: Decimal,
    max_vwap_distance_atr: Decimal,
) -> SignalQualification:
    """Evaluate fail-closed gates without changing deterministic ranking scores."""
    evaluation = _aware(evaluation_at)
    rvol_threshold = _positive_decimal(
        relative_volume_threshold, "relative_volume_threshold"
    )
    max_extension = _positive_decimal(
        max_vwap_distance_atr, "max_vwap_distance_atr"
    )
    if opening_range_minutes < 1:
        raise ValueError("opening_range_minutes must be positive")

    reasons: list[str] = []
    if candidate.health is not MarketDataHealthStatus.READY:
        reasons.append("market_data_not_ready")
    if candidate.bias is CandidateBias.LONG:
        if candidate.market_regime is not MarketRegime.TREND_UP:
            reasons.append("market_regime_not_trend_up")
        if candidate.ltp <= candidate.vwap:
            reasons.append("price_not_above_vwap")
        if candidate.ema9 <= candidate.ema20:
            reasons.append("ema9_not_above_ema20")
        if candidate.relative_volume < rvol_threshold:
            reasons.append("rvol_below_threshold")
        if candidate.relative_strength <= 0:
            reasons.append("relative_strength_not_positive")
        if not _opening_breakout_confirmed(
            candidate,
            current_session_five_minute,
            session_date=session_date,
            evaluation_at=evaluation,
            opening_range_minutes=opening_range_minutes,
        ):
            reasons.append("opening_breakout_not_confirmed")
    else:
        if candidate.market_regime is not MarketRegime.TREND_DOWN:
            reasons.append("market_regime_not_trend_down")
        if candidate.ltp >= candidate.vwap:
            reasons.append("price_not_below_vwap")
        if candidate.ema9 >= candidate.ema20:
            reasons.append("ema9_not_below_ema20")
        if candidate.relative_volume < rvol_threshold:
            reasons.append("rvol_below_threshold")
        if candidate.relative_strength >= 0:
            reasons.append("relative_strength_not_negative")
        if not _opening_breakdown_confirmed(
            candidate,
            current_session_five_minute,
            session_date=session_date,
            evaluation_at=evaluation,
            opening_range_minutes=opening_range_minutes,
        ):
            reasons.append("opening_breakdown_not_confirmed")
    if abs(candidate.vwap_distance_atr) > max_extension:
        reasons.append("vwap_atr_extension_exceeded")
    return SignalQualification(qualified=not reasons, reasons=tuple(reasons))


def _opening_breakout_confirmed(
    candidate: ScannerCandidate,
    candles: Sequence[Candle],
    *,
    session_date: date,
    evaluation_at: datetime,
    opening_range_minutes: int,
) -> bool:
    return any(
        candle.close_price > candidate.opening_range_high
        for candle in _eligible_confirmation_candles(
            candidate,
            candles,
            session_date=session_date,
            evaluation_at=evaluation_at,
            opening_range_minutes=opening_range_minutes,
        )
    )


def _opening_breakdown_confirmed(
    candidate: ScannerCandidate,
    candles: Sequence[Candle],
    *,
    session_date: date,
    evaluation_at: datetime,
    opening_range_minutes: int,
) -> bool:
    return any(
        candle.close_price < candidate.opening_range_low
        for candle in _eligible_confirmation_candles(
            candidate,
            candles,
            session_date=session_date,
            evaluation_at=evaluation_at,
            opening_range_minutes=opening_range_minutes,
        )
    )


def _eligible_confirmation_candles(
    candidate: ScannerCandidate,
    candles: Sequence[Candle],
    *,
    session_date: date,
    evaluation_at: datetime,
    opening_range_minutes: int,
) -> tuple[Candle, ...]:
    opening_range_end = datetime.combine(session_date, time(9, 15), tzinfo=IST)
    opening_range_end += timedelta(minutes=opening_range_minutes)
    return tuple(
        candle
        for candle in candles
        if candle.symbol_token == candidate.symbol_token
        and candle.interval is CandleInterval.FIVE_MINUTES
        and candle.start_at.date() == session_date
        and candle.completed
        and candle.trusted
        and candle.end_at > opening_range_end
        and candle.end_at <= evaluation_at
    )


def _positive_decimal(value: Decimal, field_name: str) -> Decimal:
    converted = Decimal(str(value))
    if not converted.is_finite() or converted <= 0:
        raise ValueError(f"{field_name} must be positive and finite")
    return converted


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation_at must be timezone-aware")
    return value.astimezone(IST)
