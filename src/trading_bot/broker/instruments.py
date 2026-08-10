"""Validated, local last-known-good Angel instrument-master cache."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from trading_bot.domain import IST, Exchange, MarketSubscription
from trading_bot.reference_data.cache import (
    CacheMetadata,
    ReferenceDataError,
    make_metadata,
    read_cache_document,
    require_fresh,
    write_atomic_cache,
)
from trading_bot.reference_data.http import download_https

ANGEL_INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
)
_SCHEMA_VERSION = 1
_MAX_DOWNLOAD_BYTES = 100_000_000
_EXCHANGE_NAMES = {
    "NSE": Exchange.NSE_CM,
    "NFO": Exchange.NSE_FO,
    "BSE": Exchange.BSE_CM,
    "BFO": Exchange.BSE_FO,
    "MCX": Exchange.MCX_FO,
    "NCX": Exchange.NCX_FO,
    "NCDEX": Exchange.NCX_FO,
    "NCO": Exchange.NCX_FO,
    "CDS": Exchange.CDE_FO,
}
_PRICE_DIVISORS = {
    Exchange.CDE_FO: Decimal("10000000"),
}
_DEFAULT_PRICE_DIVISOR = Decimal("100")


class InstrumentMasterError(RuntimeError):
    """A cached broker instrument cannot be trusted or resolved."""


class InstrumentMasterUnavailableError(InstrumentMasterError):
    """No valid last-known-good local instrument master is available."""


class InstrumentNotFoundError(InstrumentMasterError):
    """The requested token/exchange pair does not exist in the cached master."""


class InstrumentMismatchError(InstrumentMasterError):
    """Configured instrument identity conflicts with the cached broker record."""


@dataclass(frozen=True, slots=True)
class BrokerInstrument:
    exchange: Exchange
    symbol_token: str
    trading_symbol: str
    tick_size: Decimal
    lot_size: int = 1
    instrument_type: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", Exchange(self.exchange))
        token = self.symbol_token.strip()
        symbol = self.trading_symbol.strip().upper()
        if not token.isdigit():
            raise ValueError("broker symbol_token must contain only digits")
        if not symbol:
            raise ValueError("trading_symbol must not be blank")
        try:
            tick_size = Decimal(str(self.tick_size))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("tick_size must be a valid decimal") from exc
        instrument_type = self.instrument_type.strip().upper()
        if not tick_size.is_finite() or tick_size < 0:
            raise ValueError("tick_size must be finite and non-negative")
        if tick_size == 0 and instrument_type != "AMXIDX":
            raise ValueError("zero tick_size is allowed only for reference indices")
        if isinstance(self.lot_size, bool) or self.lot_size < 1:
            raise ValueError("lot_size must be a positive integer")
        object.__setattr__(self, "symbol_token", token)
        object.__setattr__(self, "trading_symbol", symbol)
        object.__setattr__(self, "tick_size", tick_size)
        object.__setattr__(self, "instrument_type", instrument_type)

    @property
    def is_normal_nse_equity(self) -> bool:
        return (
            self.exchange is Exchange.NSE_CM
            and self.trading_symbol.endswith("-EQ")
            and self.instrument_type in {"", "EQ"}
            and self.lot_size == 1
        )

    def to_cache_record(self) -> dict[str, object]:
        return {
            "exchange": self.exchange.value,
            "symbol_token": self.symbol_token,
            "trading_symbol": self.trading_symbol,
            "tick_size": str(self.tick_size),
            "lot_size": self.lot_size,
            "instrument_type": self.instrument_type,
        }


class CachedInstrumentMaster(Protocol):
    """Read only from a validated last-known-good local snapshot."""

    @property
    def cache_path(self) -> Path: ...

    @property
    def loaded_at(self) -> datetime: ...

    def require(self, subscription: MarketSubscription) -> BrokerInstrument: ...


class IntradayAllowedUniverse(Protocol):
    """Broker-provided current NSE intraday eligibility boundary."""

    def is_intraday_allowed(self, instrument: BrokerInstrument) -> bool: ...


class AngelInstrumentMasterCache:
    """Concrete validated cache for Angel One's official instrument master.

    Network access occurs only when :meth:`refresh` is explicitly called.
    Normal load/require operations are local-only and fail closed.
    """

    def __init__(
        self,
        cache_path: Path,
        *,
        max_age_seconds: float,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
        refresh_attempts: int = 3,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self._cache_path = Path(cache_path)
        self._max_age_seconds = max_age_seconds
        self._connect_timeout_seconds = connect_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._refresh_attempts = refresh_attempts
        self._metadata: CacheMetadata | None = None
        self._by_token: dict[tuple[Exchange, str], BrokerInstrument] = {}
        self._by_symbol: dict[tuple[Exchange, str], tuple[BrokerInstrument, ...]] = {}

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    @property
    def loaded_at(self) -> datetime:
        if self._metadata is None:
            raise InstrumentMasterUnavailableError("instrument master is not loaded")
        return self._metadata.fetched_at

    @property
    def metadata(self) -> CacheMetadata:
        if self._metadata is None:
            raise InstrumentMasterUnavailableError("instrument master is not loaded")
        return self._metadata

    def refresh(
        self,
        *,
        source_bytes: bytes | None = None,
        fetched_at: datetime | None = None,
    ) -> int:
        """Explicitly download/validate and atomically replace the cache."""
        payload = source_bytes
        if payload is None:
            payload = download_https(
                ANGEL_INSTRUMENT_MASTER_URL,
                connect_timeout_seconds=self._connect_timeout_seconds,
                read_timeout_seconds=self._read_timeout_seconds,
                max_attempts=self._refresh_attempts,
                max_bytes=_MAX_DOWNLOAD_BYTES,
            )
        instruments, source_record_count = _parse_source(payload)
        records = [item.to_cache_record() for item in instruments]
        timestamp = _aware(fetched_at or datetime.now(IST))
        metadata = make_metadata(
            fetched_at=timestamp,
            source_bytes=payload,
            source_url=ANGEL_INSTRUMENT_MASTER_URL,
            schema_version=_SCHEMA_VERSION,
            records=records,
            source_record_count=source_record_count,
        )
        write_atomic_cache(self._cache_path, metadata=metadata, records=records)
        self._install(metadata, instruments)
        return len(instruments)

    def load(
        self,
        *,
        now: datetime | None = None,
        require_current: bool = True,
    ) -> AngelInstrumentMasterCache:
        try:
            metadata, records = read_cache_document(self._cache_path)
            if metadata.schema_version != _SCHEMA_VERSION:
                raise ReferenceDataError("unsupported instrument cache schema")
            if require_current:
                require_fresh(
                    metadata,
                    now=_aware(now or datetime.now(IST)),
                    max_age_seconds=self._max_age_seconds,
                )
            instruments = [_instrument_from_cache(row) for row in records]
            _validate_unique(instruments)
        except (ReferenceDataError, ValueError) as exc:
            raise InstrumentMasterError(
                "Angel instrument-master cache is missing, stale, or corrupt"
            ) from exc
        self._install(metadata, instruments)
        return self

    def require(self, subscription: MarketSubscription) -> BrokerInstrument:
        if self._metadata is None:
            self.load()
        try:
            instrument = self._by_token[
                (subscription.exchange, subscription.symbol_token)
            ]
        except KeyError as exc:
            raise InstrumentNotFoundError(
                "subscription token/exchange is absent from the instrument cache"
            ) from exc
        return validate_subscription_against_instrument(subscription, instrument)

    def find_trading_symbol(
        self, exchange: Exchange, trading_symbol: str
    ) -> BrokerInstrument | None:
        if self._metadata is None:
            self.load()
        matches = self._by_symbol.get((exchange, trading_symbol.strip().upper()), ())
        if len(matches) > 1:
            raise InstrumentMismatchError(
                "instrument trading symbol is ambiguous in the authoritative cache"
            )
        return matches[0] if matches else None

    def normal_nse_equities(self) -> tuple[BrokerInstrument, ...]:
        if self._metadata is None:
            self.load()
        return tuple(
            sorted(
                (item for item in self._by_token.values() if item.is_normal_nse_equity),
                key=lambda item: item.trading_symbol,
            )
        )

    def _install(
        self, metadata: CacheMetadata, instruments: list[BrokerInstrument]
    ) -> None:
        self._metadata = metadata
        self._by_token = {
            (item.exchange, item.symbol_token): item for item in instruments
        }
        symbols: dict[tuple[Exchange, str], list[BrokerInstrument]] = {}
        for item in instruments:
            symbols.setdefault((item.exchange, item.trading_symbol), []).append(item)
        self._by_symbol = {key: tuple(value) for key, value in symbols.items()}


def validate_subscription_against_instrument(
    subscription: MarketSubscription,
    instrument: BrokerInstrument,
) -> BrokerInstrument:
    """Validate configured identity against an authoritative cached record."""
    if subscription.exchange is not instrument.exchange:
        raise InstrumentMismatchError("subscription exchange does not match cache")
    if subscription.symbol_token != instrument.symbol_token:
        raise InstrumentMismatchError("subscription token does not match cache")
    if (
        subscription.trading_symbol is not None
        and subscription.trading_symbol.upper() != instrument.trading_symbol
    ):
        raise InstrumentMismatchError(
            "subscription trading symbol does not match cache"
        )
    return instrument


def _parse_source(payload: bytes) -> tuple[list[BrokerInstrument], int]:
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstrumentMasterError("instrument source is not valid JSON") from exc
    if not isinstance(decoded, list) or not decoded:
        raise InstrumentMasterError("instrument source must be a non-empty JSON list")
    instruments: list[BrokerInstrument] = []
    identities: set[tuple[Exchange, str]] = set()
    for index, row in enumerate(decoded):
        if not isinstance(row, dict):
            raise InstrumentMasterError(f"instrument row {index} is not an object")
        try:
            exchange_name = _required_text(row, "exch_seg").upper()
            exchange = _EXCHANGE_NAMES[exchange_name]
            raw_tick_size = _nonnegative_decimal(row.get("tick_size"), "tick_size")
            divisor = _PRICE_DIVISORS.get(exchange, _DEFAULT_PRICE_DIVISOR)
            lot_size = _integer(row.get("lotsize"), "lotsize")
            token = _required_text(row, "token")
            symbol = _required_text(row, "symbol")
            instrument_type = _required_present_text(row, "instrumenttype")
            identity = (exchange, token)
            if identity in identities:
                raise InstrumentMasterError(
                    "instrument master contains a duplicate authoritative identity"
                )
            identities.add(identity)
            usable_tick_size = raw_tick_size > 0 or instrument_type == "AMXIDX"
            if token.isdigit() and usable_tick_size and lot_size > 0:
                instruments.append(
                    BrokerInstrument(
                        exchange=exchange,
                        symbol_token=token,
                        trading_symbol=symbol,
                        tick_size=raw_tick_size / divisor,
                        lot_size=lot_size,
                        instrument_type=instrument_type,
                    )
                )
        except (KeyError, ValueError) as exc:
            raise InstrumentMasterError(f"instrument row {index} is malformed") from exc
    _validate_unique(instruments)
    return instruments, len(decoded)


def _instrument_from_cache(row: object) -> BrokerInstrument:
    if not isinstance(row, dict):
        raise ValueError("instrument cache row must be an object")
    required = {
        "exchange",
        "symbol_token",
        "trading_symbol",
        "tick_size",
        "lot_size",
        "instrument_type",
    }
    if set(row) != required:
        raise ValueError("instrument cache row has an unexpected schema")
    return BrokerInstrument(
        exchange=Exchange(str(row["exchange"])),
        symbol_token=str(row["symbol_token"]),
        trading_symbol=str(row["trading_symbol"]),
        tick_size=Decimal(str(row["tick_size"])),
        lot_size=_positive_int(row["lot_size"], "lot_size"),
        instrument_type=str(row["instrument_type"]),
    )


def _validate_unique(instruments: list[BrokerInstrument]) -> None:
    tokens: set[tuple[Exchange, str]] = set()
    for item in instruments:
        token_identity = (item.exchange, item.symbol_token)
        if token_identity in tokens:
            raise InstrumentMasterError(
                "instrument master contains a duplicate authoritative identity"
            )
        tokens.add(token_identity)


def _required_text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-blank string")
    return value.strip()


def _required_present_text(row: dict[str, object], key: str) -> str:
    if key not in row or not isinstance(row[key], str):
        raise ValueError(f"{key} must be a string")
    return row[key].strip()


def _nonnegative_decimal(value: object, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return result


def _positive_int(value: object, field_name: str) -> int:
    result = _integer(value, field_name)
    if result < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return result


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    decimal_value = Decimal(str(value))
    result = int(decimal_value)
    if decimal_value != Decimal(result):
        raise ValueError(f"{field_name} must be an integer")
    return result


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(IST)
