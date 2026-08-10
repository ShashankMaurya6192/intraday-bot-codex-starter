"""Fail-closed intersection of configured, NSE, Angel, and intraday universes."""

from __future__ import annotations

from dataclasses import dataclass

from trading_bot.broker.instruments import AngelInstrumentMasterCache, BrokerInstrument
from trading_bot.broker.intraday import AngelIntradayAllowedCache

from .nifty500 import Nifty500Cache


@dataclass(frozen=True, slots=True)
class UniverseExclusion:
    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResolvedUniverse:
    instruments: tuple[BrokerInstrument, ...]
    exclusions: tuple[UniverseExclusion, ...]


def resolve_nse_equity_universe(
    nifty500: Nifty500Cache,
    instruments: AngelInstrumentMasterCache,
    intraday: AngelIntradayAllowedCache,
    *,
    configured_symbols: tuple[str, ...] = (),
) -> ResolvedUniverse:
    """Resolve authoritative identities without inventing tokens or eligibility."""
    constituents = {item.symbol: item for item in nifty500.constituents}
    requested = (
        tuple(dict.fromkeys(symbol.strip().upper() for symbol in configured_symbols))
        if configured_symbols
        else tuple(sorted(constituents))
    )
    by_base_symbol: dict[str, list[BrokerInstrument]] = {}
    for instrument in instruments.normal_nse_equities():
        base_symbol = instrument.trading_symbol.removesuffix("-EQ")
        by_base_symbol.setdefault(base_symbol, []).append(instrument)

    eligible: list[BrokerInstrument] = []
    exclusions: list[UniverseExclusion] = []
    for symbol in requested:
        if symbol not in constituents:
            exclusions.append(
                UniverseExclusion(symbol, "not_in_current_nifty500_cache")
            )
            continue
        matches = by_base_symbol.get(symbol, [])
        if not matches:
            exclusions.append(UniverseExclusion(symbol, "missing_angel_mapping"))
            continue
        if len(matches) != 1:
            exclusions.append(UniverseExclusion(symbol, "ambiguous_angel_mapping"))
            continue
        instrument = matches[0]
        if not intraday.is_intraday_allowed(instrument):
            exclusions.append(
                UniverseExclusion(symbol, "not_currently_intraday_allowed")
            )
            continue
        eligible.append(instrument)
    return ResolvedUniverse(
        instruments=tuple(sorted(eligible, key=lambda item: item.trading_symbol)),
        exclusions=tuple(
            sorted(exclusions, key=lambda item: (item.symbol, item.reason))
        ),
    )
