"""Conservative deterministic NIFTY market-regime classification."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from trading_bot.indicators import ema, session_twap

from .models import Candle, CandleInterval, InstrumentHealth


class MarketRegime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    UNKNOWN = "UNKNOWN"


def classify_market_regime(
    candles: list[Candle] | tuple[Candle, ...],
    health: InstrumentHealth | None,
    *,
    momentum_threshold_pct: Decimal = Decimal("0.05"),
    slope_lookback_candles: int = 3,
    session_date: date | None = None,
    evaluation_at: datetime | None = None,
) -> MarketRegime:
    if health is None or not health.trusted:
        return MarketRegime.UNKNOWN
    if slope_lookback_candles < 1:
        raise ValueError("slope lookback must be positive")
    usable = sorted(
        [
        candle
        for candle in candles
        if candle.interval is CandleInterval.FIVE_MINUTES
        and candle.completed
        and candle.trusted
        and (evaluation_at is None or candle.end_at <= evaluation_at)
        ],
        key=lambda candle: candle.start_at,
    )
    if len(usable) < max(20, slope_lookback_candles + 1):
        return MarketRegime.UNKNOWN
    current_session = session_date or usable[-1].start_at.date()
    session_candles = [
        candle for candle in usable if candle.start_at.date() == current_session
    ]
    anchor_result = session_twap(session_candles)
    closes = [candle.close_price for candle in usable]
    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    if not anchor_result.ready or not ema9.ready or not ema20.ready:
        return MarketRegime.UNKNOWN
    current = closes[-1]
    previous = closes[-1 - slope_lookback_candles]
    if previous <= 0:
        return MarketRegime.UNKNOWN
    slope_pct = (current / previous - 1) * Decimal("100")
    anchor_value = anchor_result.value
    ema9_value = ema9.value
    ema20_value = ema20.value
    if anchor_value is None or ema9_value is None or ema20_value is None:
        return MarketRegime.UNKNOWN
    threshold = Decimal(str(momentum_threshold_pct))
    if current > anchor_value and ema9_value > ema20_value and slope_pct >= threshold:
        return MarketRegime.TREND_UP
    if current < anchor_value and ema9_value < ema20_value and slope_pct <= -threshold:
        return MarketRegime.TREND_DOWN
    return MarketRegime.RANGE
