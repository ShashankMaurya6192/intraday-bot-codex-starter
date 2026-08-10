"""Deterministic NSE-session aligned 1m/5m candle aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from trading_bot.domain import IST, Exchange, MarketTick

from .models import Candle, CandleInterval

NSE_SESSION_OPEN = time(9, 15)
NSE_SESSION_CLOSE = time(15, 30)


@dataclass(slots=True)
class _WorkingCandle:
    exchange: Exchange
    symbol_token: str
    trading_symbol: str | None
    interval: CandleInterval
    start_at: datetime
    end_at: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    tick_count: int
    trusted: bool

    def finish(self) -> Candle:
        return Candle(
            exchange=self.exchange,
            symbol_token=self.symbol_token,
            trading_symbol=self.trading_symbol,
            interval=self.interval,
            start_at=self.start_at,
            end_at=self.end_at,
            open_price=self.open_price,
            high_price=self.high_price,
            low_price=self.low_price,
            close_price=self.close_price,
            volume=self.volume,
            tick_count=self.tick_count,
            completed=True,
            trusted=self.trusted,
        )


class SessionCandleAggregator:
    """Aggregate accepted ticks without using process-start alignment."""

    def __init__(
        self,
        intervals: tuple[CandleInterval, ...] = (
            CandleInterval.ONE_MINUTE,
            CandleInterval.FIVE_MINUTES,
        ),
    ) -> None:
        if not intervals or len(set(intervals)) != len(intervals):
            raise ValueError("intervals must be non-empty and unique")
        self._intervals = intervals
        self._working: dict[tuple[Exchange, str, CandleInterval], _WorkingCandle] = {}
        self._last_tick_at: dict[tuple[Exchange, str], datetime] = {}
        self._last_volume: dict[tuple[Exchange, str, object], int] = {}

    def process(self, tick: MarketTick, *, trusted: bool) -> tuple[Candle, ...]:
        timestamp = tick.exchange_timestamp.astimezone(IST)
        if not _inside_session(timestamp):
            return ()
        instrument_key = (tick.exchange, tick.symbol_token)
        previous_timestamp = self._last_tick_at.get(instrument_key)
        if previous_timestamp is not None and timestamp < previous_timestamp:
            return ()
        self._last_tick_at[instrument_key] = timestamp
        volume_delta = self._volume_delta(tick)

        completed: list[Candle] = []
        for interval in self._intervals:
            start_at = candle_start(timestamp, interval)
            key = (tick.exchange, tick.symbol_token, interval)
            current = self._working.get(key)
            if current is not None and start_at < current.start_at:
                continue
            if current is not None and start_at > current.start_at:
                completed.append(current.finish())
                current = None
            if current is None:
                current = _WorkingCandle(
                    exchange=tick.exchange,
                    symbol_token=tick.symbol_token,
                    trading_symbol=tick.trading_symbol,
                    interval=interval,
                    start_at=start_at,
                    end_at=start_at + timedelta(minutes=int(interval)),
                    open_price=tick.last_traded_price,
                    high_price=tick.last_traded_price,
                    low_price=tick.last_traded_price,
                    close_price=tick.last_traded_price,
                    volume=volume_delta,
                    tick_count=1,
                    trusted=trusted,
                )
                self._working[key] = current
            else:
                current.high_price = max(current.high_price, tick.last_traded_price)
                current.low_price = min(current.low_price, tick.last_traded_price)
                current.close_price = tick.last_traded_price
                current.volume += volume_delta
                current.tick_count += 1
                current.trusted = current.trusted and trusted
        return tuple(sorted(completed, key=lambda item: (item.start_at, item.interval)))

    def flush(self, *, until: datetime) -> tuple[Candle, ...]:
        timestamp = _aware(until)
        completed: list[Candle] = []
        for key, current in tuple(self._working.items()):
            if current.end_at <= timestamp:
                completed.append(current.finish())
                del self._working[key]
        return tuple(
            sorted(
                completed,
                key=lambda item: (
                    item.exchange.value,
                    item.symbol_token,
                    item.start_at,
                    item.interval,
                ),
            )
        )

    def _volume_delta(self, tick: MarketTick) -> int:
        cumulative = tick.volume_trade_for_the_day
        key = (tick.exchange, tick.symbol_token, tick.exchange_timestamp.date())
        if cumulative is None:
            return 0
        previous = self._last_volume.get(key)
        self._last_volume[key] = cumulative
        if previous is None or cumulative < previous:
            return 0
        return cumulative - previous


def candle_start(timestamp: datetime, interval: CandleInterval) -> datetime:
    timestamp = _aware(timestamp)
    session_open = datetime.combine(timestamp.date(), NSE_SESSION_OPEN, tzinfo=IST)
    elapsed_seconds = int((timestamp - session_open).total_seconds())
    if elapsed_seconds < 0:
        raise ValueError("timestamp is before the NSE cash session")
    bucket_seconds = int(interval) * 60
    return session_open + timedelta(
        seconds=(elapsed_seconds // bucket_seconds) * bucket_seconds
    )


def session_close_for(value: datetime) -> datetime:
    value = _aware(value)
    return datetime.combine(value.date(), NSE_SESSION_CLOSE, tzinfo=IST)


def _inside_session(value: datetime) -> bool:
    local_time = value.timetz().replace(tzinfo=None)
    return NSE_SESSION_OPEN <= local_time < NSE_SESSION_CLOSE


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(IST)
