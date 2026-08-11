"""Deterministic NSE-session aligned 1m/5m candle aggregation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
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
        self._last_volume: dict[tuple[Exchange, str, date], int] = {}

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

    def snapshot_completed(self, *, until: datetime) -> tuple[Candle, ...]:
        """Return as-of completed working buckets without mutating engine state."""
        timestamp = _aware(until)
        return tuple(
            sorted(
                (
                    current.finish()
                    for current in self._working.values()
                    if current.end_at <= timestamp
                ),
                key=lambda item: (
                    item.exchange.value,
                    item.symbol_token,
                    item.start_at,
                    item.interval,
                ),
            )
        )

    def checkpoint_state(self) -> dict[str, object]:
        """Return validated engine state needed for suffix-only restart catch-up."""
        return {
            "version": 1,
            "intervals": [int(interval) for interval in self._intervals],
            "working": [
                {
                    "exchange": current.exchange.value,
                    "symbol_token": current.symbol_token,
                    "trading_symbol": current.trading_symbol,
                    "interval": int(current.interval),
                    "start_at": current.start_at.isoformat(),
                    "end_at": current.end_at.isoformat(),
                    "open_price": str(current.open_price),
                    "high_price": str(current.high_price),
                    "low_price": str(current.low_price),
                    "close_price": str(current.close_price),
                    "volume": current.volume,
                    "tick_count": current.tick_count,
                    "trusted": current.trusted,
                }
                for _, current in sorted(
                    self._working.items(),
                    key=lambda item: (
                        item[0][0].value,
                        item[0][1],
                        int(item[0][2]),
                    ),
                )
            ],
            "last_tick_at": [
                {
                    "exchange": exchange.value,
                    "symbol_token": token,
                    "timestamp": timestamp.isoformat(),
                }
                for (exchange, token), timestamp in sorted(
                    self._last_tick_at.items(),
                    key=lambda item: (item[0][0].value, item[0][1]),
                )
            ],
            "last_volume": [
                {
                    "exchange": exchange.value,
                    "symbol_token": token,
                    "session_date": session_date.isoformat(),
                    "cumulative_volume": cumulative,
                }
                for (exchange, token, session_date), cumulative in sorted(
                    self._last_volume.items(),
                    key=lambda item: (
                        item[0][0].value,
                        item[0][1],
                        item[0][2],
                    ),
                )
            ],
        }

    def restore_checkpoint_state(self, payload: Mapping[str, object]) -> None:
        """Restore in-progress buckets and volume anchors from a checkpoint."""
        if self._working or self._last_tick_at or self._last_volume:
            raise ValueError(
                "candle aggregator must be empty before checkpoint restore"
            )
        if payload.get("version") != 1:
            raise ValueError("unsupported candle checkpoint version")
        raw_intervals = payload.get("intervals")
        if not isinstance(raw_intervals, list):
            raise ValueError("candle checkpoint intervals must be a list")
        intervals = tuple(CandleInterval(value) for value in raw_intervals)
        if intervals != self._intervals:
            raise ValueError("candle checkpoint intervals do not match projector")

        raw_working = _object_list(payload.get("working"), "working")
        for raw in raw_working:
            candle = Candle(
                exchange=Exchange(str(raw.get("exchange"))),
                symbol_token=str(raw.get("symbol_token", "")),
                trading_symbol=_optional_string(raw.get("trading_symbol")),
                interval=CandleInterval(_integer(raw.get("interval"), "interval")),
                start_at=_timestamp(raw.get("start_at"), "start_at"),
                end_at=_timestamp(raw.get("end_at"), "end_at"),
                open_price=Decimal(str(raw.get("open_price"))),
                high_price=Decimal(str(raw.get("high_price"))),
                low_price=Decimal(str(raw.get("low_price"))),
                close_price=Decimal(str(raw.get("close_price"))),
                volume=_non_negative_integer(raw.get("volume"), "volume"),
                tick_count=_positive_integer(raw.get("tick_count"), "tick_count"),
                completed=True,
                trusted=_boolean(raw.get("trusted"), "trusted"),
            )
            key = (candle.exchange, candle.symbol_token, candle.interval)
            if key in self._working:
                raise ValueError("duplicate working candle in checkpoint")
            self._working[key] = _WorkingCandle(
                exchange=candle.exchange,
                symbol_token=candle.symbol_token,
                trading_symbol=candle.trading_symbol,
                interval=candle.interval,
                start_at=candle.start_at,
                end_at=candle.end_at,
                open_price=candle.open_price,
                high_price=candle.high_price,
                low_price=candle.low_price,
                close_price=candle.close_price,
                volume=candle.volume,
                tick_count=candle.tick_count,
                trusted=candle.trusted,
            )

        for raw in _object_list(payload.get("last_tick_at"), "last_tick_at"):
            key = (
                Exchange(str(raw.get("exchange"))),
                str(raw.get("symbol_token", "")).strip(),
            )
            if not key[1] or key in self._last_tick_at:
                raise ValueError("invalid or duplicate last-tick checkpoint key")
            self._last_tick_at[key] = _timestamp(raw.get("timestamp"), "timestamp")

        for raw in _object_list(payload.get("last_volume"), "last_volume"):
            token = str(raw.get("symbol_token", "")).strip()
            raw_date = raw.get("session_date")
            if not token or not isinstance(raw_date, str):
                raise ValueError("invalid last-volume checkpoint key")
            try:
                session_date = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise ValueError("invalid last-volume session date") from exc
            key = (Exchange(str(raw.get("exchange"))), token, session_date)
            if key in self._last_volume:
                raise ValueError("duplicate last-volume checkpoint key")
            self._last_volume[key] = _non_negative_integer(
                raw.get("cumulative_volume"), "cumulative_volume"
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


def _object_list(value: object, field_name: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise ValueError(f"{field_name} must be a list of objects")
    return value


def _timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO timestamp")
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp") from exc


def _integer(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _non_negative_integer(value: object, field_name: str) -> int:
    converted = _integer(value, field_name)
    if converted < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return converted


def _positive_integer(value: object, field_name: str) -> int:
    converted = _integer(value, field_name)
    if converted < 1:
        raise ValueError(f"{field_name} must be positive")
    return converted


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("trading_symbol must be text or null")
    return value
