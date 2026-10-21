"""Fail-closed conversion of qualified analysis into broker-free PAPER intent."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from math import isfinite

from trading_bot.broker import BrokerInstrument, IntradayAllowedUniverse
from trading_bot.domain import IST, Exchange
from trading_bot.marketdata import Candle, CandleInterval, MarketDataHealthStatus
from trading_bot.paper import PaperTradeIntent
from trading_bot.risk import TradeSide
from trading_bot.scanner import CandidateBias, ScannerCandidate


class PaperIntentRejection(StrEnum):
    """Stable reasons why an analysis candidate cannot become a paper intent."""

    CANDIDATE_NOT_QUALIFIED = "candidate_not_qualified"
    CANDIDATE_HAS_QUALIFICATION_REASONS = "candidate_has_qualification_reasons"
    MARKET_DATA_NOT_READY = "market_data_not_ready"
    MARKET_DATA_STALE = "market_data_stale"
    OUTSIDE_ENTRY_WINDOW = "outside_entry_window"
    INSTRUMENT_IDENTITY_MISMATCH = "instrument_identity_mismatch"
    INSTRUMENT_NOT_NORMAL_NSE_EQUITY = "instrument_not_normal_nse_equity"
    INTRADAY_ELIGIBILITY_UNAVAILABLE = "intraday_eligibility_unavailable"
    INSTRUMENT_NOT_INTRADAY_ALLOWED = "instrument_not_intraday_allowed"
    LIQUIDITY_UNVERIFIED = "liquidity_unverified"
    CONFIRMATION_CANDLE_NOT_FOUND = "confirmation_candle_not_found"
    BREAKOUT_NO_LONGER_ACTIVE = "breakout_no_longer_active"
    BREAKDOWN_NO_LONGER_ACTIVE = "breakdown_no_longer_active"
    INVALID_STOP = "invalid_stop"
    STOP_DISTANCE_TOO_WIDE = "stop_distance_too_wide"


@dataclass(frozen=True, slots=True)
class PaperIntentPolicyConfig:
    """Deterministic entry/initial-stop policy for the first paper strategy."""

    opening_range_minutes: int = 15
    entry_start: time = time(9, 30)
    last_entry: time = time(14, 45)
    stale_after_seconds: float = 5.0
    stop_buffer_atr: Decimal = Decimal("0.10")
    minimum_stop_ticks: int = 2
    maximum_stop_atr: Decimal = Decimal("1.50")

    def __post_init__(self) -> None:
        if self.opening_range_minutes < 1:
            raise ValueError("opening_range_minutes must be positive")
        if self.entry_start < time(9, 30) or self.last_entry > time(14, 45):
            raise ValueError("paper intent window must stay within 09:30-14:45 IST")
        if self.entry_start >= self.last_entry:
            raise ValueError("paper intent entry_start must precede last_entry")
        if self.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        buffer_atr = _non_negative_decimal(self.stop_buffer_atr, "stop_buffer_atr")
        maximum_atr = _positive_decimal(self.maximum_stop_atr, "maximum_stop_atr")
        if self.minimum_stop_ticks < 1:
            raise ValueError("minimum_stop_ticks must be positive")
        object.__setattr__(self, "stop_buffer_atr", buffer_atr)
        object.__setattr__(self, "maximum_stop_atr", maximum_atr)


@dataclass(frozen=True, slots=True)
class PaperIntentDecision:
    """Explainable policy result; rejection never fabricates an intent."""

    accepted: bool
    intent: PaperTradeIntent | None
    rejection_reasons: tuple[PaperIntentRejection, ...]
    confirmation_candle_end: datetime | None = None
    stop_model: str = "OPENING_RANGE_FAILURE_ATR_BUFFER_V1"

    def __post_init__(self) -> None:
        if self.accepted != (self.intent is not None and not self.rejection_reasons):
            raise ValueError("paper intent decision is internally inconsistent")

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "client_tag": None if self.intent is None else self.intent.client_tag,
            "reference_entry_price": (
                None
                if self.intent is None
                else str(self.intent.reference_entry_price)
            ),
            "protective_stop": (
                None if self.intent is None else str(self.intent.protective_stop)
            ),
            "confirmation_candle_end": (
                None
                if self.confirmation_candle_end is None
                else self.confirmation_candle_end.isoformat()
            ),
            "stop_model": self.stop_model,
            "rejection_reasons": [item.value for item in self.rejection_reasons],
            "mode": "PAPER",
            "broker_order_created": False,
        }


class RelativeStrengthOpeningBreakoutV1IntentPolicy:
    """Build an idempotent PAPER intent without calling a broker or sizing it."""

    def __init__(self, config: PaperIntentPolicyConfig | None = None) -> None:
        self._config = config or PaperIntentPolicyConfig()

    def evaluate(
        self,
        candidate: ScannerCandidate,
        *,
        instrument: BrokerInstrument,
        intraday_universe: IntradayAllowedUniverse,
        current_session_five_minute: Sequence[Candle],
        evaluated_at: datetime,
        max_liquidity_quantity: int | None,
    ) -> PaperIntentDecision:
        """Return an intent only when every independent safety check succeeds."""
        evaluated = _aware(evaluated_at)
        reasons: list[PaperIntentRejection] = []
        if not candidate.qualified:
            reasons.append(PaperIntentRejection.CANDIDATE_NOT_QUALIFIED)
        if candidate.qualification_reasons:
            reasons.append(
                PaperIntentRejection.CANDIDATE_HAS_QUALIFICATION_REASONS
            )
        if candidate.health is not MarketDataHealthStatus.READY:
            reasons.append(PaperIntentRejection.MARKET_DATA_NOT_READY)
        if not isfinite(candidate.data_age_seconds) or (
            candidate.data_age_seconds < 0
            or candidate.data_age_seconds > self._config.stale_after_seconds
        ):
            reasons.append(PaperIntentRejection.MARKET_DATA_STALE)
        local_time = evaluated.timetz().replace(tzinfo=None)
        if not self._config.entry_start <= local_time <= self._config.last_entry:
            reasons.append(PaperIntentRejection.OUTSIDE_ENTRY_WINDOW)

        if not _identity_matches(candidate, instrument):
            reasons.append(PaperIntentRejection.INSTRUMENT_IDENTITY_MISMATCH)
        if not instrument.is_normal_nse_equity or instrument.tick_size <= 0:
            reasons.append(PaperIntentRejection.INSTRUMENT_NOT_NORMAL_NSE_EQUITY)
        try:
            allowed = intraday_universe.is_intraday_allowed(instrument)
        except Exception:
            allowed = False
            reasons.append(
                PaperIntentRejection.INTRADAY_ELIGIBILITY_UNAVAILABLE
            )
        else:
            if not allowed:
                reasons.append(PaperIntentRejection.INSTRUMENT_NOT_INTRADAY_ALLOWED)
        if (
            max_liquidity_quantity is None
            or isinstance(max_liquidity_quantity, bool)
            or max_liquidity_quantity < 1
        ):
            reasons.append(PaperIntentRejection.LIQUIDITY_UNVERIFIED)

        confirmation = _first_confirmation(
            candidate,
            current_session_five_minute,
            session_date=evaluated.date(),
            evaluated_at=evaluated,
            opening_range_minutes=self._config.opening_range_minutes,
        )
        if confirmation is None:
            reasons.append(PaperIntentRejection.CONFIRMATION_CANDLE_NOT_FOUND)
        if (
            candidate.bias is CandidateBias.LONG
            and candidate.ltp <= candidate.opening_range_high
        ):
            reasons.append(PaperIntentRejection.BREAKOUT_NO_LONGER_ACTIVE)
        if (
            candidate.bias is CandidateBias.SHORT
            and candidate.ltp >= candidate.opening_range_low
        ):
            reasons.append(PaperIntentRejection.BREAKDOWN_NO_LONGER_ACTIVE)

        stop = _initial_stop(candidate, instrument, self._config)
        if stop is None:
            reasons.append(PaperIntentRejection.INVALID_STOP)
        elif abs(candidate.ltp - stop) > (
            candidate.atr14 * self._config.maximum_stop_atr
        ):
            reasons.append(PaperIntentRejection.STOP_DISTANCE_TOO_WIDE)

        unique_reasons = tuple(dict.fromkeys(reasons))
        if unique_reasons or confirmation is None or stop is None:
            return PaperIntentDecision(
                accepted=False,
                intent=None,
                rejection_reasons=unique_reasons,
                confirmation_candle_end=(
                    None if confirmation is None else confirmation.end_at
                ),
            )

        side = (
            TradeSide.LONG
            if candidate.bias is CandidateBias.LONG
            else TradeSide.SHORT
        )
        intent = PaperTradeIntent(
            client_tag=_client_tag(
                evaluated.date(), instrument, candidate.bias, confirmation.end_at
            ),
            session_date=evaluated.date(),
            exchange=instrument.exchange,
            symbol_token=instrument.symbol_token,
            trading_symbol=instrument.trading_symbol,
            side=side,
            reference_entry_price=candidate.ltp,
            protective_stop=stop,
            tick_size=instrument.tick_size,
            lot_size=instrument.lot_size,
            max_liquidity_quantity=max_liquidity_quantity,
            submitted_at=evaluated,
            signal_qualified=True,
            market_data_trusted=True,
        )
        return PaperIntentDecision(
            accepted=True,
            intent=intent,
            rejection_reasons=(),
            confirmation_candle_end=confirmation.end_at,
        )


def _identity_matches(
    candidate: ScannerCandidate, instrument: BrokerInstrument
) -> bool:
    return (
        instrument.exchange is Exchange.NSE_CM
        and candidate.symbol_token == instrument.symbol_token
        and candidate.symbol.strip().upper() == instrument.trading_symbol
    )


def _first_confirmation(
    candidate: ScannerCandidate,
    candles: Sequence[Candle],
    *,
    session_date: date,
    evaluated_at: datetime,
    opening_range_minutes: int,
) -> Candle | None:
    range_end = datetime.combine(session_date, time(9, 15), tzinfo=IST)
    range_end += timedelta(minutes=opening_range_minutes)
    eligible = sorted(
        (
            candle
            for candle in candles
            if candle.exchange is Exchange.NSE_CM
            and candle.symbol_token == candidate.symbol_token
            and candle.interval is CandleInterval.FIVE_MINUTES
            and candle.start_at.date() == session_date
            and candle.completed
            and candle.trusted
            and candle.end_at > range_end
            and candle.end_at <= evaluated_at
        ),
        key=lambda candle: candle.start_at,
    )
    for candle in eligible:
        if candidate.bias is CandidateBias.LONG:
            if candle.close_price > candidate.opening_range_high:
                return candle
        elif candle.close_price < candidate.opening_range_low:
            return candle
    return None


def _initial_stop(
    candidate: ScannerCandidate,
    instrument: BrokerInstrument,
    config: PaperIntentPolicyConfig,
) -> Decimal | None:
    required_prices = (
        candidate.ltp,
        candidate.atr14,
        candidate.opening_range_high,
        candidate.opening_range_low,
    )
    if (
        any(not value.is_finite() or value <= 0 for value in required_prices)
        or instrument.tick_size <= 0
    ):
        return None
    minimum_distance = instrument.tick_size * config.minimum_stop_ticks
    buffer = candidate.atr14 * config.stop_buffer_atr
    if candidate.bias is CandidateBias.LONG:
        raw_stop = min(
            candidate.opening_range_high - buffer,
            candidate.ltp - minimum_distance,
        )
        stop = _round_to_tick(raw_stop, instrument.tick_size, ROUND_FLOOR)
        return stop if Decimal(0) < stop < candidate.ltp else None
    raw_stop = max(
        candidate.opening_range_low + buffer,
        candidate.ltp + minimum_distance,
    )
    stop = _round_to_tick(raw_stop, instrument.tick_size, ROUND_CEILING)
    return stop if stop > candidate.ltp else None


def _round_to_tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _client_tag(
    session_date: date,
    instrument: BrokerInstrument,
    bias: CandidateBias,
    confirmation_end: datetime,
) -> str:
    side = "L" if bias is CandidateBias.LONG else "S"
    return (
        f"rsobv1:{session_date:%Y%m%d}:{instrument.exchange.value}:"
        f"{instrument.symbol_token}:{side}:{confirmation_end:%H%M}"
    )


def _positive_decimal(value: Decimal, field_name: str) -> Decimal:
    converted = Decimal(str(value))
    if not converted.is_finite() or converted <= 0:
        raise ValueError(f"{field_name} must be positive and finite")
    return converted


def _non_negative_decimal(value: Decimal, field_name: str) -> Decimal:
    converted = Decimal(str(value))
    if not converted.is_finite() or converted < 0:
        raise ValueError(f"{field_name} must be non-negative and finite")
    return converted


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    return value.astimezone(IST)
