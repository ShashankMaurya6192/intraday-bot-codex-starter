from datetime import datetime
from decimal import Decimal

import pytest

from trading_bot.domain import (
    IST,
    Exchange,
    MarketSubscription,
    MarketTick,
    SubscriptionMode,
)


def test_market_tick_requires_aware_time_and_positive_price() -> None:
    common = {
        "exchange": Exchange.NSE_CM,
        "symbol_token": "3045",
        "subscription_mode": SubscriptionMode.QUOTE,
        "sequence_number": 1,
        "received_at": datetime(2026, 8, 10, 9, 30, tzinfo=IST),
        "last_traded_price": Decimal("812.25"),
    }

    with pytest.raises(ValueError, match="timezone-aware"):
        MarketTick(
            **common,
            exchange_timestamp=datetime(2026, 8, 10, 9, 30),
        )

    with pytest.raises(ValueError, match="positive"):
        MarketTick(
            **{**common, "last_traded_price": Decimal("0")},
            exchange_timestamp=datetime(2026, 8, 10, 9, 30, tzinfo=IST),
        )


def test_market_subscription_strips_values() -> None:
    subscription = MarketSubscription(
        exchange=Exchange.NSE_CM,
        symbol_token=" 3045 ",
        trading_symbol=" SBIN-EQ ",
    )

    assert subscription.symbol_token == "3045"
    assert subscription.trading_symbol == "SBIN-EQ"
