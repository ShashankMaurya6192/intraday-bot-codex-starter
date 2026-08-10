"""Conservative deterministic NIFTY market-regime classification."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from trading_bot.indicators import ema, session_vwap

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
) -> MarketRegime:
    if health is None or not health.trusted:
        return MarketRegime.UNKNOWN
    if slope_lookback_candles < 1:
        raise ValueError("slope lookback must be positive")
    usable = [
        candle
        for candle in candles
        if candle.interval is CandleInterval.FIVE_MINUTES
        and candle.completed
        and candle.trusted
    ]
    if len(usable) < max(20, slope_lookback_candles + 1):
        return MarketRegime.UNKNOWN
    vwap_result = session_vwap(usable)
    closes = [candle.close_price for candle in usable]
    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    if not vwap_result.ready or not ema9.ready or not ema20.ready:
        return MarketRegime.UNKNOWN
    current = closes[-1]
    previous = closes[-1 - slope_lookback_candles]
    if previous <= 0:
        return MarketRegime.UNKNOWN
    slope_pct = (current / previous - 1) * Decimal("100")
    vwap_value = vwap_result.value
    ema9_value = ema9.value
    ema20_value = ema20.value
    if vwap_value is None or ema9_value is None or ema20_value is None:
        return MarketRegime.UNKNOWN
    threshold = Decimal(str(momentum_threshold_pct))
    if current > vwap_value and ema9_value > ema20_value and slope_pct >= threshold:
        return MarketRegime.TREND_UP
    if current < vwap_value and ema9_value < ema20_value and slope_pct <= -threshold:
        return MarketRegime.TREND_DOWN
    return MarketRegime.RANGE
