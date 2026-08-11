"""Explicit PAPER-only yfinance fallback for research history."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_bot.broker import BrokerInstrument
from trading_bot.domain import IST, HistoricalProvider

from .candles import NSE_SESSION_CLOSE, NSE_SESSION_OPEN
from .history import (
    HistoricalBootstrapError,
    HistoricalProviderDataset,
)
from .models import Candle, CandleInterval, MarketDataProvenance

_MAX_CALENDAR_DAYS_PER_REQUEST = 7
_REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


@dataclass(frozen=True, slots=True)
class YFinanceDiagnosticResult:
    status: str
    symbol: str
    provider_symbol: str
    requested_chunks: int
    row_count: int
    complete_sessions: int
    discarded_partial_sessions: tuple[date, ...]
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    validation: str
    reason: str | None = None

    @property
    def successful(self) -> bool:
        return self.status == "SUCCESS"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "provider": HistoricalProvider.YFINANCE.value,
            "symbol": self.symbol,
            "provider_symbol": self.provider_symbol,
            "chunks": self.requested_chunks,
            "rows": self.row_count,
            "complete_sessions": self.complete_sessions,
            "discarded_partial_sessions": [
                item.isoformat() for item in self.discarded_partial_sessions
            ],
            "first_timestamp": (
                self.first_timestamp.isoformat() if self.first_timestamp else None
            ),
            "last_timestamp": (
                self.last_timestamp.isoformat() if self.last_timestamp else None
            ),
            "validation": self.validation,
            "reason": self.reason,
            "order_placement_available": False,
        }


class YFinanceHistoricalProvider:
    """Download deterministic seven-day chunks and return only complete sessions."""

    def __init__(
        self,
        *,
        downloader: Callable[..., Any] | None = None,
    ) -> None:
        self._downloader = downloader

    def fetch(
        self,
        *,
        instrument: BrokerInstrument,
        from_date: date,
        through_date: date,
        minimum_sessions: int,
    ) -> HistoricalProviderDataset:
        provider_symbol = yfinance_symbol(instrument)
        chunks, frames = self._download(
            provider_symbol=provider_symbol,
            from_date=from_date,
            through_date=through_date,
        )
        one_minute, session_dates, discarded, raw_rows = normalize_yfinance_frames(
            frames,
            instrument=instrument,
            through_date=through_date,
            minimum_sessions=minimum_sessions,
        )
        digest_rows = [
            (
                candle.start_at.isoformat(),
                str(candle.open_price),
                str(candle.high_price),
                str(candle.low_price),
                str(candle.close_price),
                candle.volume,
            )
            for candle in one_minute
        ]
        source_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "provider": HistoricalProvider.YFINANCE.value,
                    "symbol": provider_symbol,
                    "rows": digest_rows,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return HistoricalProviderDataset(
            provider=HistoricalProvider.YFINANCE,
            provenance=MarketDataProvenance.YFINANCE,
            one_minute_candles=one_minute,
            complete_session_dates=session_dates,
            source_sha256=source_sha256,
            requested_chunks=len(chunks),
            raw_rows=raw_rows,
            discarded_partial_sessions=discarded,
        )

    def diagnose(
        self,
        *,
        instrument: BrokerInstrument,
        from_date: date,
        through_date: date,
        minimum_sessions: int,
    ) -> YFinanceDiagnosticResult:
        provider_symbol = yfinance_symbol(instrument)
        chunks = yfinance_date_chunks(from_date, through_date)
        frames: tuple[Any, ...] = ()
        try:
            chunks, frames = self._download(
                provider_symbol=provider_symbol,
                from_date=from_date,
                through_date=through_date,
            )
            candles, sessions, discarded, raw_rows = normalize_yfinance_frames(
                frames,
                instrument=instrument,
                through_date=through_date,
                minimum_sessions=minimum_sessions,
            )
            return YFinanceDiagnosticResult(
                status="SUCCESS",
                symbol=instrument.trading_symbol,
                provider_symbol=provider_symbol,
                requested_chunks=len(chunks),
                row_count=raw_rows,
                complete_sessions=len(sessions),
                discarded_partial_sessions=discarded,
                first_timestamp=candles[0].start_at,
                last_timestamp=candles[-1].start_at,
                validation="PASS",
            )
        except Exception as exc:
            raw_rows = sum(_frame_length(frame) for frame in frames)
            return YFinanceDiagnosticResult(
                status="FAILED",
                symbol=instrument.trading_symbol,
                provider_symbol=provider_symbol,
                requested_chunks=len(chunks),
                row_count=raw_rows,
                complete_sessions=0,
                discarded_partial_sessions=(),
                first_timestamp=None,
                last_timestamp=None,
                validation="FAIL",
                reason=(
                    str(exc)
                    if isinstance(exc, HistoricalBootstrapError)
                    else type(exc).__name__
                ),
            )

    def _download(
        self,
        *,
        provider_symbol: str,
        from_date: date,
        through_date: date,
    ) -> tuple[tuple[tuple[date, date], ...], tuple[Any, ...]]:
        if from_date > through_date:
            raise HistoricalBootstrapError("yfinance date bounds are invalid")
        downloader = self._downloader or _default_downloader()
        chunks = yfinance_date_chunks(from_date, through_date)
        frames: list[Any] = []
        for chunk_start, chunk_end_exclusive in chunks:
            frames.append(
                downloader(
                    provider_symbol,
                    start=chunk_start.isoformat(),
                    end=chunk_end_exclusive.isoformat(),
                    interval="1m",
                    auto_adjust=False,
                    prepost=False,
                    progress=False,
                    threads=False,
                    group_by="column",
                    multi_level_index=False,
                )
            )
        return chunks, tuple(frames)


def yfinance_symbol(instrument: BrokerInstrument) -> str:
    symbol = instrument.trading_symbol.upper()
    if instrument.instrument_type == "AMXIDX":
        if symbol == "NIFTY 50":
            return "^NSEI"
        raise HistoricalBootstrapError("unsupported yfinance NSE index mapping")
    if not instrument.is_normal_nse_equity:
        raise HistoricalBootstrapError(
            "yfinance fallback supports normal NSE equities and NIFTY only"
        )
    return f"{symbol.removesuffix('-EQ')}.NS"


def yfinance_date_chunks(
    from_date: date, through_date: date
) -> tuple[tuple[date, date], ...]:
    """Return seven-calendar-day chunks with an exclusive end date."""
    if from_date > through_date:
        raise ValueError("from_date must not be after through_date")
    chunks: list[tuple[date, date]] = []
    current = from_date
    final_exclusive = through_date + timedelta(days=1)
    while current < final_exclusive:
        end_exclusive = min(
            current + timedelta(days=_MAX_CALENDAR_DAYS_PER_REQUEST),
            final_exclusive,
        )
        chunks.append((current, end_exclusive))
        current = end_exclusive
    return tuple(chunks)


def normalize_yfinance_frames(
    frames: Sequence[Any],
    *,
    instrument: BrokerInstrument,
    through_date: date,
    minimum_sessions: int,
) -> tuple[tuple[Candle, ...], tuple[date, ...], tuple[date, ...], int]:
    """Validate all rows, discard partial sessions, and keep canonical 375m days."""
    if minimum_sessions < 1:
        raise ValueError("minimum_sessions must be positive")
    candles: list[Candle] = []
    seen: set[datetime] = set()
    raw_rows = 0
    today = datetime.now(IST).date()
    for frame in frames:
        columns = _column_mapping(frame)
        for raw_timestamp, row in frame.iterrows():
            raw_rows += 1
            try:
                timestamp = _as_ist_datetime(raw_timestamp)
                if timestamp.date() > through_date or timestamp.date() >= today:
                    raise HistoricalBootstrapError(
                        "yfinance returned a current or future session"
                    )
                if timestamp.weekday() >= 5:
                    raise HistoricalBootstrapError(
                        "yfinance candle is outside an NSE weekday session"
                    )
                if timestamp.second or timestamp.microsecond:
                    raise HistoricalBootstrapError(
                        "yfinance candle is not minute-aligned"
                    )
                local_time = timestamp.timetz().replace(tzinfo=None)
                if not NSE_SESSION_OPEN <= local_time < NSE_SESSION_CLOSE:
                    raise HistoricalBootstrapError(
                        "yfinance candle is outside regular NSE hours"
                    )
                if timestamp in seen:
                    raise HistoricalBootstrapError(
                        "yfinance response has duplicate candles"
                    )
                seen.add(timestamp)
                candle = Candle(
                    exchange=instrument.exchange,
                    symbol_token=instrument.symbol_token,
                    trading_symbol=instrument.trading_symbol,
                    interval=CandleInterval.ONE_MINUTE,
                    start_at=timestamp,
                    end_at=timestamp + timedelta(minutes=1),
                    open_price=_strict_positive_decimal(row[columns["Open"]]),
                    high_price=_strict_positive_decimal(row[columns["High"]]),
                    low_price=_strict_positive_decimal(row[columns["Low"]]),
                    close_price=_strict_positive_decimal(row[columns["Close"]]),
                    volume=_strict_nonnegative_int(row[columns["Volume"]]),
                    tick_count=1,
                    completed=True,
                    trusted=True,
                    provenance=MarketDataProvenance.YFINANCE,
                )
            except HistoricalBootstrapError:
                raise
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise HistoricalBootstrapError(
                    "yfinance candle row is malformed"
                ) from exc
            candles.append(candle)
    if raw_rows == 0:
        raise HistoricalBootstrapError("yfinance returned no candle data")

    by_date: dict[date, list[Candle]] = {}
    for candle in sorted(candles, key=lambda item: item.start_at):
        by_date.setdefault(candle.start_at.date(), []).append(candle)
    expected_offsets = set(range(375))
    complete: list[Candle] = []
    complete_dates: list[date] = []
    discarded: list[date] = []
    for session_date, items in sorted(by_date.items()):
        session_open = datetime.combine(session_date, NSE_SESSION_OPEN, tzinfo=IST)
        offsets = {
            int((item.start_at - session_open).total_seconds() // 60) for item in items
        }
        if len(items) != 375 or offsets != expected_offsets:
            discarded.append(session_date)
            continue
        complete.extend(items)
        complete_dates.append(session_date)
    if len(complete_dates) < minimum_sessions:
        raise HistoricalBootstrapError(
            "yfinance history has insufficient complete prior sessions"
        )
    return (
        tuple(complete),
        tuple(complete_dates),
        tuple(discarded),
        raw_rows,
    )


def _column_mapping(frame: Any) -> dict[str, object]:
    if frame is None or not hasattr(frame, "columns") or not hasattr(frame, "iterrows"):
        raise HistoricalBootstrapError("yfinance response is not a tabular frame")
    mapping: dict[str, object] = {}
    for column in frame.columns:
        parts = (
            tuple(str(item) for item in column)
            if isinstance(column, tuple)
            else (str(column),)
        )
        for required in _REQUIRED_COLUMNS:
            if required in parts:
                if required in mapping:
                    raise HistoricalBootstrapError(
                        f"yfinance response has ambiguous {required} columns"
                    )
                mapping[required] = column
    missing = [name for name in _REQUIRED_COLUMNS if name not in mapping]
    if missing:
        raise HistoricalBootstrapError(
            "yfinance response is missing required columns: " + ",".join(missing)
        )
    return mapping


def _as_ist_datetime(value: object) -> datetime:
    converted = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    if not isinstance(converted, datetime):
        raise ValueError("yfinance timestamp is not a datetime")
    if converted.tzinfo is None or converted.utcoffset() is None:
        raise ValueError("yfinance timestamp must be timezone-aware")
    return converted.astimezone(IST)


def _strict_positive_decimal(value: object) -> Decimal:
    converted = Decimal(str(value))
    if not converted.is_finite() or converted <= 0:
        raise ValueError("price must be positive and finite")
    return converted


def _strict_nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("volume must be an integer")
    converted = Decimal(str(value))
    integer = int(converted)
    if not converted.is_finite() or converted != Decimal(integer) or integer < 0:
        raise ValueError("volume must be a non-negative integer")
    return integer


def _frame_length(frame: Any) -> int:
    try:
        return len(frame)
    except TypeError:
        return 0


def _default_downloader() -> Callable[..., Any]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise HistoricalBootstrapError(
            "yfinance is not installed; reinstall the project dependencies"
        ) from exc
    return yf.download
