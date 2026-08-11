"""Explicit, restartable SmartAPI historical-candle bootstrap."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from trading_bot.broker import (
    BrokerAuthenticationError,
    BrokerHistoricalRateLimitError,
    BrokerHistoricalResponseError,
    BrokerHistoricalTransportError,
    BrokerInstrument,
)
from trading_bot.domain import IST, Exchange, HistoricalProvider, TradingMode

from .candles import NSE_SESSION_CLOSE, NSE_SESSION_OPEN
from .models import Candle, CandleInterval, MarketDataProvenance


class HistoricalBootstrapError(RuntimeError):
    """A historical response cannot safely update the local dataset."""


_MAX_CALENDAR_DAYS_PER_REQUEST = 22
_RATE_LIMIT_MARKERS = (
    "exceeding access rate",
    "rate limit",
    "rate-limit",
    "too many requests",
    "throttl",
)
_SAFE_FIELD_PATTERN = re.compile(
    r"(?i)((?:authorization|x-api-key|x-client-code|x-feed-token|"
    r"x-privatekey|jwt[_-]?token|refresh[_-]?token|feed[_-]?token)"
    r"['\"]?\s*[:=]\s*['\"]?)(?:bearer\s+)?[^'\"\s,}]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^'\"\s,}]+")


class HistoricalDataBroker(Protocol):
    def fetch_historical_candles(
        self, parameters: Mapping[str, object]
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class HistoricalBootstrapStatus:
    exchange: Exchange
    symbol_token: str
    trading_symbol: str
    from_date: date
    through_date: date
    fetched_at: datetime
    sessions_found: int
    one_minute_candles: int
    five_minute_candles: int
    source_sha256: str
    provider: str = HistoricalProvider.ANGEL.value
    provenance: MarketDataProvenance = MarketDataProvenance.ANGEL_HISTORICAL

    @property
    def cache_age_seconds(self) -> float:
        return max(0.0, (datetime.now(IST) - self.fetched_at).total_seconds())


class HistoricalBootstrapStore(Protocol):
    def load_historical_bootstrap_status(
        self, *, exchange: Exchange, symbol_token: str
    ) -> HistoricalBootstrapStatus | None: ...

    def replace_historical_dataset(
        self,
        *,
        instrument: BrokerInstrument,
        one_minute_candles: Sequence[Candle],
        five_minute_candles: Sequence[Candle],
        complete_session_dates: Sequence[date],
        from_date: date,
        through_date: date,
        fetched_at: datetime,
        source_sha256: str,
        build_rvol_baselines: bool,
        provider: str = HistoricalProvider.ANGEL.value,
        provenance: MarketDataProvenance = MarketDataProvenance.ANGEL_HISTORICAL,
    ) -> HistoricalBootstrapStatus: ...


@dataclass(frozen=True, slots=True)
class HistoricalProviderDataset:
    provider: HistoricalProvider
    provenance: MarketDataProvenance
    one_minute_candles: tuple[Candle, ...]
    complete_session_dates: tuple[date, ...]
    source_sha256: str
    requested_chunks: int
    raw_rows: int
    discarded_partial_sessions: tuple[date, ...] = ()


class HistoricalFallbackProvider(Protocol):
    def fetch(
        self,
        *,
        instrument: BrokerInstrument,
        from_date: date,
        through_date: date,
        minimum_sessions: int,
    ) -> HistoricalProviderDataset: ...


@dataclass(frozen=True, slots=True)
class HistoricalSymbolResult:
    symbol: str
    status: str
    sessions_found: int
    cache_age_seconds: float | None
    reason_category: str | None = None
    reason: str | None = None
    provider: str | None = None
    provenance: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "sessions_found": self.sessions_found,
            "cache_age_seconds": self.cache_age_seconds,
            "reason_category": self.reason_category,
            "reason": self.reason,
            "provider": self.provider,
            "provenance": self.provenance,
        }


@dataclass(frozen=True, slots=True)
class HistoricalBootstrapResult:
    status: str
    requested_symbols: int
    completed_symbols: int
    failed_symbols: int
    sessions_found: int
    cache_age_seconds: float | None
    skipped_after_abort: int
    rate_limited_symbol: str | None
    reason_category: str | None
    operator_action: str | None
    symbols: tuple[HistoricalSymbolResult, ...]
    fallback_used: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "requested": self.requested_symbols,
            "completed": self.completed_symbols,
            "failed": self.failed_symbols,
            "sessions_found": self.sessions_found,
            "cache_age_seconds": self.cache_age_seconds,
            "skipped_after_abort": self.skipped_after_abort,
            "rate_limited_symbol": self.rate_limited_symbol,
            "reason_category": self.reason_category,
            "operator_action": self.operator_action,
            "symbols": [item.to_dict() for item in self.symbols],
            "fallback_used": self.fallback_used,
            "order_placement_available": False,
        }


@dataclass(frozen=True, slots=True)
class HistoricalDiagnosticResult:
    status: str
    symbol: str
    session_date: date
    broker_status: object | None
    broker_message: str | None
    error_code: str | None
    row_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    reason_category: str | None = None
    reason: str | None = None

    @property
    def successful(self) -> bool:
        return self.status == "SUCCESS"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "symbol": self.symbol,
            "session_date": self.session_date.isoformat(),
            "broker_status": self.broker_status,
            "broker_message": self.broker_message,
            "error_code": self.error_code,
            "row_count": self.row_count,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "reason_category": self.reason_category,
            "reason": self.reason,
            "order_placement_available": False,
        }


class HistoricalBootstrapService:
    """Fetch complete prior sessions without running on record/scan hot paths."""

    def __init__(
        self,
        broker: HistoricalDataBroker | None,
        store: HistoricalBootstrapStore,
        *,
        lookback_days: int = 30,
        minimum_sessions: int = 15,
        request_delay_seconds: float = 0.5,
        max_attempts: int = 3,
        retry_initial_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 8.0,
        sleeper=time.sleep,
        provider: HistoricalProvider = HistoricalProvider.ANGEL,
        fallback_enabled: bool = False,
        fallback_provider: HistoricalFallbackProvider | None = None,
        trading_mode: TradingMode = TradingMode.PAPER,
    ) -> None:
        if not 22 <= lookback_days <= 30:
            raise ValueError("historical lookback must be between 22 and 30 days")
        if minimum_sessions < 1:
            raise ValueError("historical minimum sessions must be positive")
        if request_delay_seconds < 0:
            raise ValueError("historical request delay must be non-negative")
        if max_attempts < 1:
            raise ValueError("historical max attempts must be positive")
        if retry_initial_delay_seconds > retry_max_delay_seconds:
            raise ValueError("historical retry delay bounds are invalid")
        provider = HistoricalProvider(provider)
        trading_mode = TradingMode(trading_mode)
        if provider is HistoricalProvider.ANGEL and broker is None:
            raise ValueError("Angel historical provider requires a broker adapter")
        if provider is HistoricalProvider.YFINANCE and not fallback_enabled:
            raise ValueError("yfinance history requires explicit fallback opt-in")
        if provider is HistoricalProvider.AUTO and broker is None:
            raise ValueError("automatic history requires an Angel broker adapter")
        if provider is HistoricalProvider.YFINANCE or (
            provider is HistoricalProvider.AUTO and fallback_enabled
        ):
            if fallback_provider is None:
                raise ValueError("configured yfinance history provider is unavailable")
            if trading_mode is not TradingMode.PAPER:
                raise ValueError("yfinance history is PAPER-only")
        self._broker = broker
        self._store = store
        self._lookback_days = lookback_days
        self._minimum_sessions = minimum_sessions
        self._request_delay = request_delay_seconds
        self._max_attempts = max_attempts
        self._retry_initial = retry_initial_delay_seconds
        self._retry_max = retry_max_delay_seconds
        self._sleeper = sleeper
        self._provider = provider
        self._fallback_enabled = fallback_enabled
        self._fallback_provider = fallback_provider
        self._trading_mode = trading_mode

    def bootstrap(
        self,
        instruments: Sequence[BrokerInstrument],
        *,
        benchmark: BrokerInstrument,
        now: datetime,
        force: bool = False,
    ) -> HistoricalBootstrapResult:
        now_ist = _aware(now)
        if benchmark.instrument_type != "AMXIDX":
            raise HistoricalBootstrapError("benchmark must be an AMXIDX instrument")
        all_instruments = tuple(
            sorted(
                {
                    (item.exchange, item.symbol_token): item
                    for item in (*instruments, benchmark)
                }.values(),
                key=lambda item: item.trading_symbol,
            )
        )
        through_date = now_ist.date() - timedelta(days=1)
        from_date = through_date - timedelta(days=self._lookback_days - 1)
        results: list[HistoricalSymbolResult] = []
        request_number = 0
        angel_throttled = self._provider is HistoricalProvider.YFINANCE
        fallback_used = self._provider is HistoricalProvider.YFINANCE
        fallback_trigger_symbol: str | None = None
        for instrument_index, instrument in enumerate(all_instruments):
            previous = self._store.load_historical_bootstrap_status(
                exchange=instrument.exchange,
                symbol_token=instrument.symbol_token,
            )
            if (
                not force
                and previous is not None
                and previous.from_date <= from_date
                and previous.through_date >= through_date
                and previous.sessions_found >= self._minimum_sessions
            ):
                results.append(
                    HistoricalSymbolResult(
                        symbol=instrument.trading_symbol,
                        status="SKIPPED_CURRENT",
                        sessions_found=previous.sessions_found,
                        cache_age_seconds=max(
                            0.0, (now_ist - previous.fetched_at).total_seconds()
                        ),
                        provider=previous.provider,
                        provenance=previous.provenance.value,
                    )
                )
                continue
            try:
                use_fallback = (
                    self._provider is HistoricalProvider.YFINANCE or angel_throttled
                )
                if use_fallback:
                    dataset = self._fetch_fallback(
                        instrument=instrument,
                        from_date=from_date,
                        through_date=through_date,
                    )
                    fallback_used = True
                else:
                    responses: list[Mapping[str, object]] = []
                    for chunk_start, chunk_end in _historical_date_chunks(
                        from_date, through_date
                    ):
                        if request_number:
                            self._sleeper(self._request_delay)
                        request_number += 1
                        parameters = _historical_parameters(
                            instrument, chunk_start, chunk_end
                        )
                        responses.append(self._fetch_with_retries(parameters))
                    response = _combine_historical_responses(responses)
                    one_minute, session_dates = normalize_historical_response(
                        response,
                        instrument=instrument,
                        minimum_sessions=self._minimum_sessions,
                    )
                    dataset = HistoricalProviderDataset(
                        provider=HistoricalProvider.ANGEL,
                        provenance=MarketDataProvenance.ANGEL_HISTORICAL,
                        one_minute_candles=one_minute,
                        complete_session_dates=session_dates,
                        source_sha256=hashlib.sha256(
                            json.dumps(
                                responses,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ).encode("utf-8")
                        ).hexdigest(),
                        requested_chunks=len(responses),
                        raw_rows=len(response.get("data") or ()),
                    )
                five_minute = aggregate_historical_five_minute(
                    dataset.one_minute_candles
                )
                status = self._store.replace_historical_dataset(
                    instrument=instrument,
                    one_minute_candles=dataset.one_minute_candles,
                    five_minute_candles=five_minute,
                    complete_session_dates=dataset.complete_session_dates,
                    from_date=from_date,
                    through_date=through_date,
                    fetched_at=now_ist,
                    source_sha256=dataset.source_sha256,
                    build_rvol_baselines=instrument.instrument_type != "AMXIDX",
                    provider=dataset.provider.value,
                    provenance=dataset.provenance,
                )
                results.append(
                    HistoricalSymbolResult(
                        symbol=instrument.trading_symbol,
                        status="COMPLETED",
                        sessions_found=status.sessions_found,
                        cache_age_seconds=0.0,
                        provider=status.provider,
                        provenance=status.provenance.value,
                    )
                )
            except BrokerHistoricalRateLimitError as exc:
                if (
                    self._provider is HistoricalProvider.AUTO
                    and self._fallback_enabled
                    and self._trading_mode is TradingMode.PAPER
                ):
                    angel_throttled = True
                    fallback_used = True
                    fallback_trigger_symbol = instrument.trading_symbol
                    try:
                        dataset = self._fetch_fallback(
                            instrument=instrument,
                            from_date=from_date,
                            through_date=through_date,
                        )
                        five_minute = aggregate_historical_five_minute(
                            dataset.one_minute_candles
                        )
                        status = self._store.replace_historical_dataset(
                            instrument=instrument,
                            one_minute_candles=dataset.one_minute_candles,
                            five_minute_candles=five_minute,
                            complete_session_dates=dataset.complete_session_dates,
                            from_date=from_date,
                            through_date=through_date,
                            fetched_at=now_ist,
                            source_sha256=dataset.source_sha256,
                            build_rvol_baselines=(
                                instrument.instrument_type != "AMXIDX"
                            ),
                            provider=dataset.provider.value,
                            provenance=dataset.provenance,
                        )
                        results.append(
                            HistoricalSymbolResult(
                                symbol=instrument.trading_symbol,
                                status="COMPLETED",
                                sessions_found=status.sessions_found,
                                cache_age_seconds=0.0,
                                provider=status.provider,
                                provenance=status.provenance.value,
                            )
                        )
                    except Exception as fallback_exc:
                        results.append(
                            HistoricalSymbolResult(
                                symbol=instrument.trading_symbol,
                                status="FAILED",
                                sessions_found=(
                                    previous.sessions_found if previous else 0
                                ),
                                cache_age_seconds=(
                                    max(
                                        0.0,
                                        (now_ist - previous.fetched_at).total_seconds(),
                                    )
                                    if previous
                                    else None
                                ),
                                reason_category=_failure_category(fallback_exc),
                                reason=_safe_failure_reason(fallback_exc),
                                provider=HistoricalProvider.YFINANCE.value,
                            )
                        )
                    continue
                results.append(
                    HistoricalSymbolResult(
                        symbol=instrument.trading_symbol,
                        status="RATE_LIMITED",
                        sessions_found=previous.sessions_found if previous else 0,
                        cache_age_seconds=(
                            max(0.0, (now_ist - previous.fetched_at).total_seconds())
                            if previous
                            else None
                        ),
                        reason_category="RATE_LIMITED",
                        reason=_safe_failure_reason(exc),
                    )
                )
                return _bootstrap_result(
                    requested_symbols=len(all_instruments),
                    results=results,
                    status="ABORTED_RATE_LIMIT",
                    skipped_after_abort=len(all_instruments) - instrument_index - 1,
                    rate_limited_symbol=instrument.trading_symbol,
                    reason_category="RATE_LIMITED",
                    operator_action=(
                        "Wait for the Angel Historical API rate-limit window to "
                        "reset, then rerun the same command. Completed symbols "
                        "will be skipped unless --force is supplied."
                    ),
                    fallback_used=fallback_used,
                )
            except BrokerAuthenticationError:
                raise
            except Exception as exc:
                results.append(
                    HistoricalSymbolResult(
                        symbol=instrument.trading_symbol,
                        status="FAILED",
                        sessions_found=previous.sessions_found if previous else 0,
                        cache_age_seconds=(
                            max(0.0, (now_ist - previous.fetched_at).total_seconds())
                            if previous
                            else None
                        ),
                        reason_category=_failure_category(exc),
                        reason=_safe_failure_reason(exc),
                    )
                )

        return _bootstrap_result(
            requested_symbols=len(all_instruments),
            results=results,
            status=(
                "COMPLETED_WITH_FAILURES"
                if any(item.status == "FAILED" for item in results)
                else ("COMPLETED_WITH_FALLBACK" if fallback_used else "COMPLETED")
            ),
            fallback_used=fallback_used,
            rate_limited_symbol=fallback_trigger_symbol,
        )

    def _fetch_fallback(
        self,
        *,
        instrument: BrokerInstrument,
        from_date: date,
        through_date: date,
    ) -> HistoricalProviderDataset:
        if self._fallback_provider is None:
            raise HistoricalBootstrapError("historical fallback is unavailable")
        return self._fallback_provider.fetch(
            instrument=instrument,
            from_date=from_date,
            through_date=through_date,
            minimum_sessions=self._minimum_sessions,
        )

    def _fetch_with_retries(
        self, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        last_error: Exception | None = None
        if self._broker is None:
            raise HistoricalBootstrapError("Angel historical broker is unavailable")
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._broker.fetch_historical_candles(parameters)
                if response.get("status") is not True:
                    broker_message = _safe_response_field(response.get("message"))
                    error_code = _safe_response_field(response.get("errorcode"))
                    if _looks_rate_limited(broker_message, error_code):
                        raise BrokerHistoricalRateLimitError(
                            "Angel Historical API rate limit: "
                            "Access denied because of exceeding access rate",
                            broker_status=response.get("status"),
                            broker_message=broker_message,
                            error_code=error_code,
                        )
                    raise BrokerHistoricalResponseError(
                        "Angel Historical API rejected the request",
                        broker_status=response.get("status"),
                        broker_message=broker_message,
                        error_code=error_code,
                    )
                return response
            except (
                BrokerHistoricalRateLimitError,
                BrokerHistoricalResponseError,
                BrokerAuthenticationError,
            ):
                raise
            except (BrokerHistoricalTransportError, OSError) as exc:
                last_error = exc
                if attempt < self._max_attempts:
                    delay = min(
                        self._retry_initial * (2 ** (attempt - 1)), self._retry_max
                    )
                    self._sleeper(delay)
            except Exception as exc:
                raise BrokerHistoricalTransportError(
                    "historical request failed with a non-retryable unexpected error"
                ) from exc
        detail = _safe_failure_reason(last_error) if last_error else "unknown"
        raise BrokerHistoricalTransportError(
            f"historical transport failed after {self._max_attempts} attempts: {detail}"
        ) from last_error


def run_historical_diagnostic(
    broker: HistoricalDataBroker,
    *,
    instrument: BrokerInstrument,
    session_date: date,
) -> HistoricalDiagnosticResult:
    """Make exactly one historical request for one prior session, without retries."""
    parameters = _historical_parameters(instrument, session_date, session_date)
    try:
        response = broker.fetch_historical_candles(parameters)
    except BrokerHistoricalRateLimitError as exc:
        return _diagnostic_error(
            instrument,
            session_date,
            exc,
            status="ABORTED_RATE_LIMIT",
            reason_category="RATE_LIMITED",
        )
    except BrokerAuthenticationError as exc:
        return _diagnostic_error(
            instrument,
            session_date,
            exc,
            status="AUTHENTICATION_FAILED",
            reason_category="AUTHENTICATION",
        )
    except BrokerHistoricalResponseError as exc:
        return _diagnostic_error(
            instrument,
            session_date,
            exc,
            status="REJECTED",
            reason_category="BROKER_RESPONSE_ERROR",
        )
    except BrokerHistoricalTransportError as exc:
        return _diagnostic_error(
            instrument,
            session_date,
            exc,
            status="FAILED",
            reason_category="TRANSIENT_FAILURE",
        )
    except Exception as exc:
        return HistoricalDiagnosticResult(
            status="FAILED",
            symbol=instrument.trading_symbol,
            session_date=session_date,
            broker_status=None,
            broker_message=None,
            error_code=None,
            row_count=0,
            first_timestamp=None,
            last_timestamp=None,
            reason_category="UNEXPECTED_ERROR",
            reason=type(exc).__name__,
        )

    broker_status = _safe_broker_status(response.get("status"))
    broker_message = _safe_response_field(response.get("message")) or None
    error_code = _safe_response_field(response.get("errorcode")) or None
    if broker_status is not True:
        category = (
            "RATE_LIMITED"
            if _looks_rate_limited(broker_message or "", error_code or "")
            else "BROKER_RESPONSE_ERROR"
        )
        return HistoricalDiagnosticResult(
            status=("ABORTED_RATE_LIMIT" if category == "RATE_LIMITED" else "REJECTED"),
            symbol=instrument.trading_symbol,
            session_date=session_date,
            broker_status=broker_status,
            broker_message=broker_message,
            error_code=error_code,
            row_count=0,
            first_timestamp=None,
            last_timestamp=None,
            reason_category=category,
            reason=(
                "Angel Historical API rate limit"
                if category == "RATE_LIMITED"
                else "Angel Historical API rejected the request"
            ),
        )
    rows = response.get("data")
    if not isinstance(rows, list):
        return HistoricalDiagnosticResult(
            status="MALFORMED_RESPONSE",
            symbol=instrument.trading_symbol,
            session_date=session_date,
            broker_status=broker_status,
            broker_message=broker_message,
            error_code=error_code,
            row_count=0,
            first_timestamp=None,
            last_timestamp=None,
            reason_category="BROKER_RESPONSE_ERROR",
            reason="Angel Historical API returned malformed candle data",
        )
    return HistoricalDiagnosticResult(
        status="SUCCESS",
        symbol=instrument.trading_symbol,
        session_date=session_date,
        broker_status=broker_status,
        broker_message=broker_message,
        error_code=error_code,
        row_count=len(rows),
        first_timestamp=_row_timestamp(rows[0]) if rows else None,
        last_timestamp=_row_timestamp(rows[-1]) if rows else None,
    )


def _diagnostic_error(
    instrument: BrokerInstrument,
    session_date: date,
    exc: Exception,
    *,
    status: str,
    reason_category: str,
) -> HistoricalDiagnosticResult:
    return HistoricalDiagnosticResult(
        status=status,
        symbol=instrument.trading_symbol,
        session_date=session_date,
        broker_status=_safe_broker_status(getattr(exc, "broker_status", None)),
        broker_message=(
            _safe_response_field(getattr(exc, "broker_message", None)) or None
        ),
        error_code=_safe_response_field(getattr(exc, "error_code", None)) or None,
        row_count=0,
        first_timestamp=None,
        last_timestamp=None,
        reason_category=reason_category,
        reason=_safe_failure_reason(exc),
    )


def _bootstrap_result(
    *,
    requested_symbols: int,
    results: Sequence[HistoricalSymbolResult],
    status: str,
    skipped_after_abort: int = 0,
    rate_limited_symbol: str | None = None,
    reason_category: str | None = None,
    operator_action: str | None = None,
    fallback_used: bool = False,
) -> HistoricalBootstrapResult:
    completed = tuple(
        item for item in results if item.status in {"COMPLETED", "SKIPPED_CURRENT"}
    )
    failed = tuple(
        item for item in results if item.status in {"FAILED", "RATE_LIMITED"}
    )
    ages = [
        item.cache_age_seconds
        for item in completed
        if item.cache_age_seconds is not None
    ]
    return HistoricalBootstrapResult(
        status=status,
        requested_symbols=requested_symbols,
        completed_symbols=len(completed),
        failed_symbols=len(failed),
        sessions_found=sum(item.sessions_found for item in completed),
        cache_age_seconds=max(ages) if ages else None,
        skipped_after_abort=skipped_after_abort,
        rate_limited_symbol=rate_limited_symbol,
        reason_category=reason_category,
        operator_action=operator_action,
        symbols=tuple(results),
        fallback_used=fallback_used,
    )


def _failure_category(exc: Exception) -> str:
    if isinstance(exc, BrokerHistoricalResponseError):
        return "BROKER_RESPONSE_ERROR"
    if isinstance(exc, BrokerHistoricalTransportError):
        return "TRANSIENT_FAILURE"
    if isinstance(exc, HistoricalBootstrapError):
        return "VALIDATION_ERROR"
    return "UNEXPECTED_ERROR"


def _safe_failure_reason(exc: Exception) -> str:
    if isinstance(
        exc,
        (
            BrokerHistoricalRateLimitError,
            BrokerHistoricalResponseError,
            BrokerHistoricalTransportError,
            BrokerAuthenticationError,
            HistoricalBootstrapError,
        ),
    ):
        return _safe_response_field(str(exc))
    return type(exc).__name__


def _safe_response_field(value: object) -> str:
    if value is None:
        return ""
    message = _SAFE_FIELD_PATTERN.sub(r"\1***", str(value))
    message = _BEARER_PATTERN.sub("Bearer ***", message)
    message = " ".join(message.split())
    return message[:240]


def _safe_broker_status(value: object) -> object | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_response_field(value)
    return type(value).__name__


def _looks_rate_limited(message: str, error_code: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in _RATE_LIMIT_MARKERS) or (
        error_code.strip().upper() in {"429", "RATE_LIMITED", "TOO_MANY_REQUESTS"}
    )


def _historical_parameters(
    instrument: BrokerInstrument, from_date: date, through_date: date
) -> dict[str, object]:
    return {
        "exchange": _historical_exchange(instrument.exchange),
        "symboltoken": instrument.symbol_token,
        "interval": "ONE_MINUTE",
        "fromdate": f"{from_date.isoformat()} 09:15",
        "todate": f"{through_date.isoformat()} 15:30",
    }


def _historical_date_chunks(
    from_date: date, through_date: date
) -> tuple[tuple[date, date], ...]:
    chunks: list[tuple[date, date]] = []
    chunk_start = from_date
    while chunk_start <= through_date:
        chunk_end = min(
            chunk_start + timedelta(days=_MAX_CALENDAR_DAYS_PER_REQUEST - 1),
            through_date,
        )
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return tuple(chunks)


def _combine_historical_responses(
    responses: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    combined_rows: list[object] = []
    for response in responses:
        rows = response.get("data")
        if not isinstance(rows, list):
            return {"status": True, "data": rows}
        combined_rows.extend(rows)
    return {"status": True, "data": combined_rows}


def _row_timestamp(row: object) -> str | None:
    if not isinstance(row, (list, tuple)) or not row:
        return None
    return _safe_response_field(row[0]) or None


def normalize_historical_response(
    response: Mapping[str, object],
    *,
    instrument: BrokerInstrument,
    minimum_sessions: int,
) -> tuple[tuple[Candle, ...], tuple[date, ...]]:
    """Validate every row and retain only fully contiguous NSE cash sessions."""
    if response.get("status") is not True:
        raise HistoricalBootstrapError("historical response status is not successful")
    rows = response.get("data")
    if not isinstance(rows, list) or not rows:
        raise HistoricalBootstrapError("historical response has no candle data")
    candles: list[Candle] = []
    seen: set[datetime] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) != 6:
            raise HistoricalBootstrapError(
                f"historical candle row {index} has an unexpected schema"
            )
        try:
            raw_start = datetime.fromisoformat(str(row[0]))
            if raw_start.tzinfo is None or raw_start.utcoffset() is None:
                raise ValueError("historical timestamp must be timezone-aware")
            start_at = raw_start.astimezone(IST)
            volume = _strict_nonnegative_int(row[5])
            candle = Candle(
                exchange=instrument.exchange,
                symbol_token=instrument.symbol_token,
                trading_symbol=instrument.trading_symbol,
                interval=CandleInterval.ONE_MINUTE,
                start_at=start_at,
                end_at=start_at + timedelta(minutes=1),
                open_price=Decimal(str(row[1])),
                high_price=Decimal(str(row[2])),
                low_price=Decimal(str(row[3])),
                close_price=Decimal(str(row[4])),
                volume=volume,
                tick_count=1,
                completed=True,
                trusted=True,
                provenance=MarketDataProvenance.ANGEL_HISTORICAL,
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise HistoricalBootstrapError(
                f"historical candle row {index} is malformed"
            ) from exc
        if start_at.second or start_at.microsecond:
            raise HistoricalBootstrapError("historical candle is not minute-aligned")
        local_time = start_at.timetz().replace(tzinfo=None)
        if not NSE_SESSION_OPEN <= local_time < NSE_SESSION_CLOSE:
            raise HistoricalBootstrapError("historical candle is outside NSE hours")
        if start_at in seen:
            raise HistoricalBootstrapError("historical response has duplicate candles")
        seen.add(start_at)
        candles.append(candle)

    by_date: dict[date, list[Candle]] = {}
    for candle in sorted(candles, key=lambda item: item.start_at):
        by_date.setdefault(candle.start_at.date(), []).append(candle)
    expected_offsets = set(range(375))
    complete: list[Candle] = []
    session_dates: list[date] = []
    for session_date, session_candles in sorted(by_date.items()):
        session_open = datetime.combine(session_date, NSE_SESSION_OPEN, tzinfo=IST)
        offsets = {
            int((candle.start_at - session_open).total_seconds() // 60)
            for candle in session_candles
        }
        if offsets != expected_offsets or len(session_candles) != 375:
            raise HistoricalBootstrapError(
                f"historical response contains a partial session: {session_date}"
            )
        complete.extend(session_candles)
        session_dates.append(session_date)
    if len(session_dates) < minimum_sessions:
        raise HistoricalBootstrapError(
            "historical response has insufficient complete prior sessions"
        )
    return tuple(complete), tuple(session_dates)


def aggregate_historical_five_minute(
    one_minute_candles: Sequence[Candle],
) -> tuple[Candle, ...]:
    """Derive trusted five-minute candles from contiguous canonical one-minute data."""
    grouped: dict[tuple[Exchange, str, datetime], list[Candle]] = {}
    for candle in one_minute_candles:
        session_open = datetime.combine(
            candle.start_at.date(), NSE_SESSION_OPEN, tzinfo=IST
        )
        offset = int((candle.start_at - session_open).total_seconds() // 60)
        bucket = session_open + timedelta(minutes=(offset // 5) * 5)
        grouped.setdefault((candle.exchange, candle.symbol_token, bucket), []).append(
            candle
        )
    result: list[Candle] = []
    for (_, _, start_at), items in sorted(grouped.items(), key=lambda item: item[0][2]):
        items.sort(key=lambda item: item.start_at)
        if len(items) != 5 or any(
            current.end_at != following.start_at
            for current, following in zip(items[:-1], items[1:], strict=True)
        ):
            raise HistoricalBootstrapError(
                "canonical one-minute history cannot form a complete five-minute candle"
            )
        result.append(
            Candle(
                exchange=items[0].exchange,
                symbol_token=items[0].symbol_token,
                trading_symbol=items[0].trading_symbol,
                interval=CandleInterval.FIVE_MINUTES,
                start_at=start_at,
                end_at=start_at + timedelta(minutes=5),
                open_price=items[0].open_price,
                high_price=max(item.high_price for item in items),
                low_price=min(item.low_price for item in items),
                close_price=items[-1].close_price,
                volume=sum(item.volume for item in items),
                tick_count=sum(item.tick_count for item in items),
                completed=True,
                trusted=True,
                provenance=items[0].provenance,
            )
        )
    return tuple(result)


def _strict_nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("volume must be an integer")
    decimal_value = Decimal(str(value))
    integer = int(decimal_value)
    if decimal_value != Decimal(integer) or integer < 0:
        raise ValueError("volume must be a non-negative integer")
    return integer


def _historical_exchange(exchange: Exchange) -> str:
    if exchange is not Exchange.NSE_CM:
        raise HistoricalBootstrapError("Phase 2.1 history supports NSE cash only")
    return "NSE"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("historical bootstrap time must be timezone-aware")
    return value.astimezone(IST)
