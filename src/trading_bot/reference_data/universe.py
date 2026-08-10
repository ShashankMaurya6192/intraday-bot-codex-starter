"""Fail-closed intersection of configured, NSE, Angel, and intraday universes."""

from __future__ import annotations

from dataclasses import dataclass

from trading_bot.broker.instruments import AngelInstrumentMasterCache, BrokerInstrument
from trading_bot.broker.intraday import AngelIntradayAllowedCache
from trading_bot.domain import Exchange, MarketSubscription

from .nifty500 import Nifty500Cache


@dataclass(frozen=True, slots=True)
class UniverseExclusion:
    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResolvedUniverse:
    instruments: tuple[BrokerInstrument, ...]
    exclusions: tuple[UniverseExclusion, ...]

    @property
    def expected_eligibility_exclusions(self) -> tuple[UniverseExclusion, ...]:
        return tuple(
            item
            for item in self.exclusions
            if item.reason == "not_currently_intraday_allowed"
        )

    @property
    def mapping_failures(self) -> tuple[UniverseExclusion, ...]:
        return tuple(
            item
            for item in self.exclusions
            if item.reason != "not_currently_intraday_allowed"
        )


class ScannerSubscriptionError(RuntimeError):
    """Scanner subscriptions cannot be built from trusted local references."""


@dataclass(frozen=True, slots=True)
class ScannerSubscriptionPlan:
    subscriptions: tuple[MarketSubscription, ...]
    requested_count: int
    eligible_count: int
    expected_ineligible_count: int
    mapping_failure_count: int
    benchmark_count: int
    exclusions: tuple[UniverseExclusion, ...]


def resolve_nse_equity_universe(
    nifty500: Nifty500Cache,
    instruments: AngelInstrumentMasterCache,
    intraday: AngelIntradayAllowedCache,
    *,
    configured_symbols: tuple[str, ...] = (),
) -> ResolvedUniverse:
    """Resolve authoritative identities without inventing tokens or eligibility."""
    mapped = resolve_nse_equity_mappings(
        nifty500,
        instruments,
        configured_symbols=configured_symbols,
    )
    eligible: list[BrokerInstrument] = []
    exclusions = list(mapped.exclusions)
    for instrument in mapped.instruments:
        if not intraday.is_intraday_allowed(instrument):
            exclusions.append(
                UniverseExclusion(
                    instrument.trading_symbol.removesuffix("-EQ"),
                    "not_currently_intraday_allowed",
                )
            )
            continue
        eligible.append(instrument)
    return ResolvedUniverse(
        instruments=tuple(sorted(eligible, key=lambda item: item.trading_symbol)),
        exclusions=tuple(
            sorted(exclusions, key=lambda item: (item.symbol, item.reason))
        ),
    )


def resolve_nse_equity_mappings(
    nifty500: Nifty500Cache,
    instruments: AngelInstrumentMasterCache,
    *,
    configured_symbols: tuple[str, ...] = (),
) -> ResolvedUniverse:
    """Resolve NIFTY 500 equities without applying current intraday eligibility."""
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
        eligible.append(matches[0])
    return ResolvedUniverse(
        instruments=tuple(sorted(eligible, key=lambda item: item.trading_symbol)),
        exclusions=tuple(
            sorted(exclusions, key=lambda item: (item.symbol, item.reason))
        ),
    )


def build_scanner_subscription_plan(
    nifty500: Nifty500Cache,
    instruments: AngelInstrumentMasterCache,
    intraday: AngelIntradayAllowedCache,
    *,
    benchmark_trading_symbol: str,
    configured_symbols: tuple[str, ...] = (),
    maximum_subscriptions: int = 1_000,
) -> ScannerSubscriptionPlan:
    """Build one validated equity-plus-benchmark WebSocket subscription set."""
    if maximum_subscriptions < 1:
        raise ValueError("maximum_subscriptions must be positive")
    universe = resolve_nse_equity_universe(
        nifty500,
        instruments,
        intraday,
        configured_symbols=configured_symbols,
    )
    benchmark = instruments.find_trading_symbol(
        Exchange.NSE_CM, benchmark_trading_symbol
    )
    if benchmark is None or benchmark.instrument_type != "AMXIDX":
        raise ScannerSubscriptionError(
            "authoritative NIFTY benchmark mapping is missing or invalid"
        )

    subscriptions = [
        MarketSubscription(
            exchange=item.exchange,
            symbol_token=item.symbol_token,
            trading_symbol=item.trading_symbol,
        )
        for item in universe.instruments
    ]
    subscriptions.append(
        MarketSubscription(
            exchange=benchmark.exchange,
            symbol_token=benchmark.symbol_token,
            trading_symbol=benchmark.trading_symbol,
        )
    )
    deduplicated = tuple(
        sorted(
            {
                (item.exchange, item.symbol_token): item for item in subscriptions
            }.values(),
            key=lambda item: (item.exchange.value, item.symbol_token),
        )
    )
    if len(deduplicated) > maximum_subscriptions:
        raise ScannerSubscriptionError(
            "scanner universe exceeds the 1000-token SmartAPI WebSocket limit"
        )
    requested = (
        len(configured_symbols) if configured_symbols else len(nifty500.constituents)
    )
    return ScannerSubscriptionPlan(
        subscriptions=deduplicated,
        requested_count=requested,
        eligible_count=len(universe.instruments),
        expected_ineligible_count=len(universe.expected_eligibility_exclusions),
        mapping_failure_count=len(universe.mapping_failures),
        benchmark_count=1,
        exclusions=universe.exclusions,
    )
