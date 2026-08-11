"""Causal same-day recovery from broker-maintained session fields."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from trading_bot.domain import IST, Exchange, MarketTick

from .candles import NSE_SESSION_OPEN
from .models import (
    Candle,
    CandleInterval,
    MarketDataProvenance,
    SessionReference,
)

SESSION_SNAPSHOT_TOLERANCE = timedelta(seconds=2)
_FIRST_FIVE_END = time(9, 20)
_OPENING_RANGE_END = time(9, 30)


@dataclass(slots=True)
class SessionReferenceAccumulator:
    """Retain causal reference points without synthesizing minute bars."""

    latest: dict[tuple[Exchange, str], SessionReference]
    before_first_five: dict[tuple[Exchange, str], SessionReference]
    before_opening_range: dict[tuple[Exchange, str], SessionReference]

    def __init__(self) -> None:
        self.latest = {}
        self.before_first_five = {}
        self.before_opening_range = {}

    def observe(self, tick: MarketTick, *, trusted: bool) -> None:
        reference = session_reference_from_tick(tick, trusted=trusted)
        key = (tick.exchange, tick.symbol_token)
        if reference is None:
            self.latest.pop(key, None)
            if _within_pre_boundary(tick.exchange_timestamp, _FIRST_FIVE_END):
                self.before_first_five.pop(key, None)
            if _within_pre_boundary(tick.exchange_timestamp, _OPENING_RANGE_END):
                self.before_opening_range.pop(key, None)
            return
        self.latest[key] = reference
        if _within_pre_boundary(reference.observed_at, _FIRST_FIVE_END):
            self.before_first_five[key] = reference
        if _within_pre_boundary(reference.observed_at, _OPENING_RANGE_END):
            self.before_opening_range[key] = reference


def session_reference_from_tick(
    tick: MarketTick, *, trusted: bool
) -> SessionReference | None:
    """Return a safe reference only when one trusted tick proves all session OHLC."""
    if not trusted or any(
        value is None for value in (tick.open_price, tick.high_price, tick.low_price)
    ):
        return None
    average = tick.average_traded_price
    if average is not None and average <= 0:
        average = None
    try:
        return SessionReference(
            exchange=tick.exchange,
            symbol_token=tick.symbol_token,
            trading_symbol=tick.trading_symbol,
            session_date=tick.exchange_timestamp.date(),
            observed_at=tick.exchange_timestamp,
            session_open=tick.open_price,
            session_high=tick.high_price,
            session_low=tick.low_price,
            last_traded_price=tick.last_traded_price,
            average_traded_price=average,
            cumulative_volume=tick.volume_trade_for_the_day,
            provenance=MarketDataProvenance.ANGEL_SESSION_FIELDS,
            trusted=True,
        )
    except ValueError:
        return None


def recover_first_five_minute_candles(
    candles: list[Candle],
    references: dict[tuple[Exchange, str], SessionReference],
    *,
    evaluation_at: datetime | None,
) -> list[Candle]:
    """Replace a late partial 09:15 bucket only when a pre-09:20 snapshot proves it."""
    opening_minutes: dict[tuple[Exchange, str, date], set[int]] = {}
    for candle in candles:
        if candle.interval is not CandleInterval.ONE_MINUTE:
            continue
        key = (candle.exchange, candle.symbol_token, candle.start_at.date())
        session_open = datetime.combine(key[2], NSE_SESSION_OPEN, tzinfo=IST)
        offset = int((candle.start_at - session_open).total_seconds() // 60)
        if 0 <= offset < 5:
            opening_minutes.setdefault(key, set()).add(offset)
    incomplete_openings = {
        key for key, offsets in opening_minutes.items() if offsets != set(range(5))
    }
    recovered = [
        candle
        for candle in candles
        if not (
            candle.interval is CandleInterval.FIVE_MINUTES
            and candle.start_at.time() == NSE_SESSION_OPEN
            and (candle.exchange, candle.symbol_token, candle.start_at.date())
            in incomplete_openings
        )
    ]
    for key, reference in references.items():
        session_open = datetime.combine(reference.session_date, NSE_SESSION_OPEN, IST)
        first_five_end = session_open + timedelta(minutes=5)
        if evaluation_at is not None and evaluation_at < first_five_end:
            continue
        local_one_minute = [
            candle
            for candle in recovered
            if (candle.exchange, candle.symbol_token) == key
            and candle.interval is CandleInterval.ONE_MINUTE
            and candle.start_at.date() == reference.session_date
        ]
        local_opening_offsets = {
            int((candle.start_at - session_open).total_seconds() // 60)
            for candle in local_one_minute
            if session_open <= candle.start_at < first_five_end
        }
        if local_opening_offsets == set(range(5)):
            continue
        recovered = [
            candle
            for candle in recovered
            if not (
                (candle.exchange, candle.symbol_token) == key
                and candle.interval is CandleInterval.FIVE_MINUTES
                and candle.start_at == session_open
            )
        ]
        recovered.append(
            Candle(
                exchange=reference.exchange,
                symbol_token=reference.symbol_token,
                trading_symbol=reference.trading_symbol,
                interval=CandleInterval.FIVE_MINUTES,
                start_at=session_open,
                end_at=first_five_end,
                open_price=reference.session_open,
                high_price=reference.session_high,
                low_price=reference.session_low,
                close_price=reference.last_traded_price,
                volume=reference.cumulative_volume or 0,
                tick_count=1,
                completed=True,
                trusted=True,
                provenance=MarketDataProvenance.ANGEL_SESSION_FIELDS,
            )
        )
    return recovered


def opening_references_visible(
    references: dict[tuple[Exchange, str], SessionReference],
    *,
    evaluation_at: datetime | None,
) -> tuple[SessionReference, ...]:
    if not references:
        return ()
    visible: list[SessionReference] = []
    for reference in references.values():
        boundary = datetime.combine(reference.session_date, _OPENING_RANGE_END, IST)
        if evaluation_at is None or evaluation_at >= boundary:
            visible.append(reference)
    return tuple(sorted(visible, key=_reference_sort_key))


def _within_pre_boundary(observed_at: datetime, boundary_time: time) -> bool:
    boundary = datetime.combine(observed_at.date(), boundary_time, IST)
    return boundary - SESSION_SNAPSHOT_TOLERANCE <= observed_at < boundary


def _reference_sort_key(reference: SessionReference) -> tuple[str, str]:
    return reference.exchange.value, reference.symbol_token
