"""Atomic local cache for authoritative NSE Indices NIFTY 500 constituents."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from trading_bot.domain import IST

from .cache import (
    CacheMetadata,
    ReferenceDataError,
    make_metadata,
    read_cache_document,
    require_fresh,
    write_atomic_cache,
)
from .http import ReferenceDataDownloadError, download_https

NIFTY500_CONSTITUENTS_URL = (
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
)
NIFTY500_OFFICIAL_FALLBACK_URL = (
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
)
_SCHEMA_VERSION = 1
_MAX_DOWNLOAD_BYTES = 5_000_000
_REQUIRED_COLUMNS = {"Company Name", "Industry", "Symbol", "Series", "ISIN Code"}
_OFFICIAL_HTTPS_HOSTS = {
    "nsearchives.nseindia.com",
    "archives.nseindia.com",
    "www.niftyindices.com",
}


class Nifty500CacheError(ReferenceDataError):
    """The NIFTY 500 constituent cache cannot be trusted."""


@dataclass(frozen=True, slots=True)
class NiftyConstituent:
    company_name: str
    industry: str
    symbol: str
    series: str
    isin: str

    def __post_init__(self) -> None:
        for field_name in ("company_name", "industry", "symbol", "series", "isin"):
            value = getattr(self, field_name).strip()
            if not value:
                raise ValueError(f"{field_name} must not be blank")
            if field_name in {"symbol", "series", "isin"}:
                value = value.upper()
            object.__setattr__(self, field_name, value)
        if self.series != "EQ":
            raise ValueError("only normal NSE EQ constituents are accepted")

    def to_cache_record(self) -> dict[str, object]:
        return {
            "company_name": self.company_name,
            "industry": self.industry,
            "symbol": self.symbol,
            "series": self.series,
            "isin": self.isin,
        }


class Nifty500Cache:
    """Explicitly refreshed, local-only NIFTY 500 universe boundary."""

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
        self.cache_path = Path(cache_path)
        self._max_age_seconds = max_age_seconds
        self._connect_timeout_seconds = connect_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._refresh_attempts = refresh_attempts
        self._metadata: CacheMetadata | None = None
        self._constituents: tuple[NiftyConstituent, ...] = ()

    @property
    def metadata(self) -> CacheMetadata:
        if self._metadata is None:
            raise Nifty500CacheError("NIFTY 500 cache is not loaded")
        return self._metadata

    @property
    def constituents(self) -> tuple[NiftyConstituent, ...]:
        if self._metadata is None:
            self.load()
        return self._constituents

    def refresh(
        self,
        *,
        source: str = NIFTY500_CONSTITUENTS_URL,
        source_bytes: bytes | None = None,
        fetched_at: datetime | None = None,
    ) -> int:
        payload = source_bytes
        actual_source = source
        if payload is None:
            if source.lower().startswith("https://"):
                if urlsplit(source).hostname not in _OFFICIAL_HTTPS_HOSTS:
                    raise Nifty500CacheError(
                        "NIFTY 500 HTTPS source must be an official NSE host"
                    )
                sources = (
                    (NIFTY500_CONSTITUENTS_URL, NIFTY500_OFFICIAL_FALLBACK_URL)
                    if source == NIFTY500_CONSTITUENTS_URL
                    else (source,)
                )
                last_error: ReferenceDataDownloadError | None = None
                for candidate in sources:
                    try:
                        payload = download_https(
                            candidate,
                            connect_timeout_seconds=self._connect_timeout_seconds,
                            read_timeout_seconds=self._read_timeout_seconds,
                            max_attempts=self._refresh_attempts,
                            max_bytes=_MAX_DOWNLOAD_BYTES,
                        )
                        actual_source = candidate
                        break
                    except ReferenceDataDownloadError as exc:
                        last_error = exc
                if payload is None:
                    raise Nifty500CacheError(
                        "all official NIFTY 500 sources failed"
                    ) from last_error
            else:
                try:
                    payload = Path(source).expanduser().resolve().read_bytes()
                except OSError as exc:
                    raise Nifty500CacheError(
                        "NIFTY 500 import source is unavailable"
                    ) from exc
        constituents = _parse_csv(payload)
        records = [item.to_cache_record() for item in constituents]
        timestamp = _aware(fetched_at or datetime.now(IST))
        metadata = make_metadata(
            fetched_at=timestamp,
            source_bytes=payload,
            source_url=actual_source,
            schema_version=_SCHEMA_VERSION,
            records=records,
        )
        write_atomic_cache(self.cache_path, metadata=metadata, records=records)
        self._install(metadata, constituents)
        return len(constituents)

    def load(
        self,
        *,
        now: datetime | None = None,
        require_current: bool = True,
    ) -> Nifty500Cache:
        try:
            metadata, rows = read_cache_document(self.cache_path)
            if metadata.schema_version != _SCHEMA_VERSION:
                raise ReferenceDataError("unsupported NIFTY 500 cache schema")
            if require_current:
                require_fresh(
                    metadata,
                    now=_aware(now or datetime.now(IST)),
                    max_age_seconds=self._max_age_seconds,
                )
            constituents = [_from_cache(row) for row in rows]
            _validate_unique(constituents)
        except (ReferenceDataError, ValueError) as exc:
            raise Nifty500CacheError(
                "NIFTY 500 cache is missing, stale, or corrupt"
            ) from exc
        self._install(metadata, constituents)
        return self

    def _install(
        self, metadata: CacheMetadata, constituents: list[NiftyConstituent]
    ) -> None:
        self._metadata = metadata
        self._constituents = tuple(sorted(constituents, key=lambda item: item.symbol))


def _parse_csv(payload: bytes) -> list[NiftyConstituent]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise Nifty500CacheError("NIFTY 500 CSV is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not _REQUIRED_COLUMNS.issubset(reader.fieldnames):
        raise Nifty500CacheError("NIFTY 500 CSV has an unexpected schema")
    constituents: list[NiftyConstituent] = []
    for index, row in enumerate(reader, start=2):
        try:
            constituents.append(
                NiftyConstituent(
                    company_name=row["Company Name"],
                    industry=row["Industry"],
                    symbol=row["Symbol"],
                    series=row["Series"],
                    isin=row["ISIN Code"],
                )
            )
        except (KeyError, ValueError, AttributeError) as exc:
            raise Nifty500CacheError(f"NIFTY 500 CSV row {index} is malformed") from exc
    if not constituents:
        raise Nifty500CacheError("NIFTY 500 CSV has no constituents")
    _validate_unique(constituents)
    return constituents


def _from_cache(row: object) -> NiftyConstituent:
    required = {"company_name", "industry", "symbol", "series", "isin"}
    if not isinstance(row, dict) or set(row) != required:
        raise ValueError("NIFTY 500 cache row has an unexpected schema")
    return NiftyConstituent(
        company_name=str(row["company_name"]),
        industry=str(row["industry"]),
        symbol=str(row["symbol"]),
        series=str(row["series"]),
        isin=str(row["isin"]),
    )


def _validate_unique(constituents: list[NiftyConstituent]) -> None:
    symbols: set[str] = set()
    isins: set[str] = set()
    for item in constituents:
        if item.symbol in symbols or item.isin in isins:
            raise Nifty500CacheError(
                "NIFTY 500 data contains a duplicate authoritative identity"
            )
        symbols.add(item.symbol)
        isins.add(item.isin)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(IST)
