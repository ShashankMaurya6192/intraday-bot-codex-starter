"""Current-session Angel NSE intraday-allowed snapshot cache."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_bot.domain import IST, Exchange
from trading_bot.reference_data.cache import (
    CacheMetadata,
    ReferenceDataError,
    make_metadata,
    read_cache_document,
    require_fresh,
    write_atomic_cache,
)

from .instruments import BrokerInstrument, InstrumentMasterError

ANGEL_NSE_INTRADAY_URL = (
    "https://apiconnect.angelone.in/rest/secure/angelbroking/marketData/v1/nseIntraday"
)
_SCHEMA_VERSION = 1


class IntradayUniverseError(InstrumentMasterError):
    """The current Angel intraday-allowed snapshot cannot be trusted."""


@dataclass(frozen=True, slots=True)
class IntradayAllowedRecord:
    symbol: str
    multiplier: Decimal

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol must not be blank")
        try:
            multiplier = Decimal(str(self.multiplier))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("multiplier must be a decimal") from exc
        if not multiplier.is_finite() or multiplier <= 0:
            raise ValueError("multiplier must be positive")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "multiplier", multiplier)

    def to_cache_record(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "multiplier": str(self.multiplier),
        }


class AngelIntradayAllowedCache:
    """Local, same-IST-date snapshot of Angel's authenticated NSE endpoint."""

    def __init__(self, cache_path: Path, *, max_age_seconds: float) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self.cache_path = Path(cache_path)
        self._max_age_seconds = max_age_seconds
        self._metadata: CacheMetadata | None = None
        self._records: dict[str, IntradayAllowedRecord] = {}

    @property
    def metadata(self) -> CacheMetadata:
        if self._metadata is None:
            raise IntradayUniverseError("intraday universe is not loaded")
        return self._metadata

    def refresh_from_response(
        self,
        response: Mapping[str, object],
        *,
        fetched_at: datetime | None = None,
    ) -> int:
        records = _parse_response(response)
        cached = [item.to_cache_record() for item in records]
        source_bytes = json.dumps(
            response, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        timestamp = _aware(fetched_at or datetime.now(IST))
        metadata = make_metadata(
            fetched_at=timestamp,
            source_bytes=source_bytes,
            source_url=ANGEL_NSE_INTRADAY_URL,
            schema_version=_SCHEMA_VERSION,
            records=cached,
        )
        write_atomic_cache(self.cache_path, metadata=metadata, records=cached)
        self._install(metadata, records)
        return len(records)

    def load(
        self,
        *,
        now: datetime | None = None,
        require_current: bool = True,
    ) -> AngelIntradayAllowedCache:
        try:
            metadata, rows = read_cache_document(self.cache_path)
            if metadata.schema_version != _SCHEMA_VERSION:
                raise ReferenceDataError("unsupported intraday cache schema")
            if require_current:
                require_fresh(
                    metadata,
                    now=_aware(now or datetime.now(IST)),
                    max_age_seconds=self._max_age_seconds,
                    same_ist_date=True,
                )
            records = [_from_cache(row) for row in rows]
            _unique(records)
        except (ReferenceDataError, ValueError) as exc:
            raise IntradayUniverseError(
                "intraday-allowed cache is missing, stale, or corrupt"
            ) from exc
        self._install(metadata, records)
        return self

    def is_intraday_allowed(self, instrument: BrokerInstrument) -> bool:
        if self._metadata is None:
            self.load()
        return (
            instrument.exchange is Exchange.NSE_CM
            and instrument.is_normal_nse_equity
            and instrument.trading_symbol.removesuffix("-EQ") in self._records
        )

    def multiplier_for(self, instrument: BrokerInstrument) -> Decimal | None:
        if not self.is_intraday_allowed(instrument):
            return None
        return self._records[instrument.trading_symbol.removesuffix("-EQ")].multiplier

    def _install(
        self, metadata: CacheMetadata, records: list[IntradayAllowedRecord]
    ) -> None:
        self._metadata = metadata
        self._records = {item.symbol: item for item in records}


def _parse_response(response: Mapping[str, object]) -> list[IntradayAllowedRecord]:
    if response.get("status") is not True:
        raise IntradayUniverseError("Angel rejected the intraday-universe request")
    data = response.get("data")
    if not isinstance(data, list) or not data:
        raise IntradayUniverseError("Angel intraday response data is malformed")
    records: list[IntradayAllowedRecord] = []
    for index, row in enumerate(data):
        if not isinstance(row, Mapping):
            raise IntradayUniverseError(f"intraday row {index} is malformed")
        exchange = _compatible_field(row, "exchange", "Exchange")
        if not isinstance(exchange, str) or exchange.strip().upper() != "NSE":
            raise IntradayUniverseError(f"intraday row {index} is not an NSE record")
        symbol = _compatible_field(row, "symbol", "SymbolName")
        try:
            multiplier = _compatible_field(row, "multiplier", "Multiplier")
        except IntradayUniverseError as exc:
            raise IntradayUniverseError(
                f"intraday row {index} has an invalid multiplier"
            ) from exc
        if not isinstance(symbol, str):
            raise IntradayUniverseError(f"intraday row {index} has no symbol")
        try:
            records.append(IntradayAllowedRecord(symbol, Decimal(str(multiplier))))
        except (InvalidOperation, ValueError) as exc:
            raise IntradayUniverseError(
                f"intraday row {index} has an invalid multiplier"
            ) from exc
    _unique(records)
    return records


def _compatible_field(
    row: Mapping[str, object], documented_key: str, compatibility_key: str
) -> object:
    documented_present = documented_key in row
    compatibility_present = compatibility_key in row
    if not documented_present and not compatibility_present:
        raise IntradayUniverseError(
            f"intraday row is missing required field {documented_key}"
        )
    if documented_present and compatibility_present:
        documented = row[documented_key]
        compatibility = row[compatibility_key]
        if str(documented).strip().upper() != str(compatibility).strip().upper():
            raise IntradayUniverseError(
                f"intraday row has conflicting field {documented_key}"
            )
    return row[documented_key] if documented_present else row[compatibility_key]


def _from_cache(row: object) -> IntradayAllowedRecord:
    if not isinstance(row, dict) or set(row) != {"symbol", "multiplier"}:
        raise ValueError("intraday cache row has an unexpected schema")
    return IntradayAllowedRecord(
        symbol=str(row["symbol"]),
        multiplier=Decimal(str(row["multiplier"])),
    )


def _unique(records: list[IntradayAllowedRecord]) -> None:
    symbols: set[str] = set()
    for record in records:
        if record.symbol in symbols:
            raise IntradayUniverseError(
                "intraday snapshot contains a duplicate authoritative symbol"
            )
        symbols.add(record.symbol)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(IST)
