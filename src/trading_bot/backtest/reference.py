"""Immutable reference boundary required for causal strategy replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from trading_bot.broker import BrokerInstrument
from trading_bot.domain import IST, Exchange


class ReplayReferenceSnapshotError(RuntimeError):
    """A replay reference snapshot is absent, corrupt, or conflicting."""


@dataclass(frozen=True, slots=True)
class ReplayReferenceSnapshot:
    """Exact scanner identities and source hashes frozen for one IST session."""

    session_date: date
    captured_at: datetime
    instrument_master_sha256: str
    intraday_universe_sha256: str
    nifty500_sha256: str
    configured_symbols: tuple[str, ...]
    instruments: tuple[BrokerInstrument, ...]
    benchmark: BrokerInstrument
    exclusions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        captured = _aware(self.captured_at)
        if captured.date() != self.session_date:
            raise ValueError("reference snapshot must be captured on its IST session")
        object.__setattr__(self, "captured_at", captured)
        for name in (
            "instrument_master_sha256",
            "intraday_universe_sha256",
            "nifty500_sha256",
        ):
            value = str(getattr(self, name)).strip().lower()
            if len(value) != 64 or any(
                char not in "0123456789abcdef" for char in value
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
            object.__setattr__(self, name, value)
        configured = tuple(symbol.strip().upper() for symbol in self.configured_symbols)
        if any(not symbol for symbol in configured) or len(configured) != len(
            set(configured)
        ):
            raise ValueError("configured snapshot symbols must be non-blank and unique")
        object.__setattr__(self, "configured_symbols", configured)
        instruments = tuple(
            sorted(self.instruments, key=lambda item: item.trading_symbol)
        )
        identities = {
            (item.exchange, item.symbol_token, item.trading_symbol)
            for item in instruments
        }
        if not instruments or len(identities) != len(instruments):
            raise ValueError("snapshot instruments must be non-empty and unique")
        if any(not item.is_normal_nse_equity for item in instruments):
            raise ValueError("snapshot instruments must be normal NSE equities")
        object.__setattr__(self, "instruments", instruments)
        if (
            self.benchmark.exchange is not Exchange.NSE_CM
            or self.benchmark.instrument_type != "AMXIDX"
        ):
            raise ValueError("snapshot benchmark must be an NSE AMXIDX instrument")
        exclusions = tuple(
            sorted({item.strip() for item in self.exclusions if item.strip()})
        )
        object.__setattr__(self, "exclusions", exclusions)

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()

    @property
    def payload_json(self) -> str:
        return json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "instrument_master_sha256": self.instrument_master_sha256,
            "intraday_universe_sha256": self.intraday_universe_sha256,
            "nifty500_sha256": self.nifty500_sha256,
            "configured_symbols": list(self.configured_symbols),
            "instruments": [_instrument_payload(item) for item in self.instruments],
            "benchmark": _instrument_payload(self.benchmark),
            "exclusions": list(self.exclusions),
        }

    @classmethod
    def from_payload(
        cls,
        *,
        session_date: date,
        captured_at: datetime,
        payload: object,
    ) -> ReplayReferenceSnapshot:
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ReplayReferenceSnapshotError(
                "unsupported replay reference snapshot schema"
            )
        try:
            configured = _string_list(payload.get("configured_symbols"))
            exclusions = _string_list(payload.get("exclusions"))
            raw_instruments = payload.get("instruments")
            if not isinstance(raw_instruments, list):
                raise ValueError("instruments must be a list")
            instruments = tuple(
                _instrument_from_payload(item) for item in raw_instruments
            )
            benchmark = _instrument_from_payload(payload.get("benchmark"))
            return cls(
                session_date=session_date,
                captured_at=captured_at,
                instrument_master_sha256=str(payload["instrument_master_sha256"]),
                intraday_universe_sha256=str(payload["intraday_universe_sha256"]),
                nifty500_sha256=str(payload["nifty500_sha256"]),
                configured_symbols=configured,
                instruments=instruments,
                benchmark=benchmark,
                exclusions=exclusions,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplayReferenceSnapshotError(
                "replay reference snapshot payload is malformed"
            ) from exc


class SnapshotSourceCache(Protocol):
    @property
    def metadata(self): ...


class FrozenIntradayAllowedUniverse:
    """Exact allow-list captured at recorder startup; performs no I/O."""

    def __init__(self, instruments: tuple[BrokerInstrument, ...]) -> None:
        self._allowed = frozenset(
            (item.exchange, item.symbol_token, item.trading_symbol)
            for item in instruments
        )

    def is_intraday_allowed(self, instrument: BrokerInstrument) -> bool:
        return (
            instrument.exchange,
            instrument.symbol_token,
            instrument.trading_symbol,
        ) in self._allowed


def build_replay_reference_snapshot(
    *,
    session_date: date,
    captured_at: datetime,
    instrument_cache: SnapshotSourceCache,
    intraday_cache: SnapshotSourceCache,
    nifty500_cache: SnapshotSourceCache,
    configured_symbols: tuple[str, ...],
    instruments: tuple[BrokerInstrument, ...],
    benchmark: BrokerInstrument,
    exclusions: tuple[str, ...] = (),
) -> ReplayReferenceSnapshot:
    """Freeze already-validated cache identities without refreshing any source."""
    return ReplayReferenceSnapshot(
        session_date=session_date,
        captured_at=captured_at,
        instrument_master_sha256=str(instrument_cache.metadata.sha256),
        intraday_universe_sha256=str(intraday_cache.metadata.sha256),
        nifty500_sha256=str(nifty500_cache.metadata.sha256),
        configured_symbols=configured_symbols,
        instruments=instruments,
        benchmark=benchmark,
        exclusions=exclusions,
    )


def _instrument_payload(instrument: BrokerInstrument) -> dict[str, object]:
    return {
        "exchange": instrument.exchange.value,
        "symbol_token": instrument.symbol_token,
        "trading_symbol": instrument.trading_symbol,
        "tick_size": str(instrument.tick_size),
        "lot_size": instrument.lot_size,
        "instrument_type": instrument.instrument_type,
    }


def _instrument_from_payload(payload: object) -> BrokerInstrument:
    if not isinstance(payload, dict):
        raise ValueError("instrument payload must be an object")
    expected = {
        "exchange",
        "symbol_token",
        "trading_symbol",
        "tick_size",
        "lot_size",
        "instrument_type",
    }
    if set(payload) != expected:
        raise ValueError("instrument payload has an unexpected schema")
    lot_size = payload["lot_size"]
    if not isinstance(lot_size, int) or isinstance(lot_size, bool):
        raise ValueError("instrument lot_size must be an integer")
    return BrokerInstrument(
        exchange=Exchange(str(payload["exchange"])),
        symbol_token=str(payload["symbol_token"]),
        trading_symbol=str(payload["trading_symbol"]),
        tick_size=Decimal(str(payload["tick_size"])),
        lot_size=lot_size,
        instrument_type=str(payload["instrument_type"]),
    )


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("snapshot string collection is malformed")
    return tuple(value)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("snapshot captured_at must be timezone-aware")
    return value.astimezone(IST)
