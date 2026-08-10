from decimal import Decimal

import pytest

from trading_bot.broker import (
    BrokerInstrument,
    InstrumentMismatchError,
    validate_subscription_against_instrument,
)
from trading_bot.domain import Exchange, MarketSubscription


def test_broker_instrument_requires_valid_identity_and_tick_size() -> None:
    instrument = BrokerInstrument(
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        tick_size=Decimal("0.05"),
    )

    assert instrument.tick_size == Decimal("0.05")

    with pytest.raises(ValueError, match="tick_size"):
        BrokerInstrument(
            exchange=Exchange.NSE_CM,
            symbol_token="3045",
            trading_symbol="SBIN-EQ",
            tick_size=Decimal("0"),
        )


def test_subscription_identity_must_match_cached_instrument() -> None:
    instrument = BrokerInstrument(
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        tick_size=Decimal("0.05"),
    )

    validated = validate_subscription_against_instrument(
        MarketSubscription(Exchange.NSE_CM, "3045", "SBIN-EQ"),
        instrument,
    )
    assert validated is instrument

    with pytest.raises(InstrumentMismatchError, match="trading symbol"):
        validate_subscription_against_instrument(
            MarketSubscription(Exchange.NSE_CM, "3045", "GUESSED-EQ"),
            instrument,
        )
