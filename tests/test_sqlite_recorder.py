from datetime import datetime
from decimal import Decimal

from trading_bot.domain import (
    IST,
    Exchange,
    FeedEvent,
    FeedEventType,
    MarketTick,
    SubscriptionMode,
)
from trading_bot.storage import SQLiteMarketRecorder


def _tick(sequence_number: int = 17) -> MarketTick:
    return MarketTick(
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        subscription_mode=SubscriptionMode.QUOTE,
        sequence_number=sequence_number,
        exchange_timestamp=datetime(2026, 8, 10, 9, 30, 5, tzinfo=IST),
        received_at=datetime(2026, 8, 10, 9, 30, 5, 10_000, tzinfo=IST),
        last_traded_price=Decimal("812.25"),
        last_traded_quantity=10,
        average_traded_price=Decimal("810.05"),
        volume_trade_for_the_day=12_345,
        total_buy_quantity=Decimal("999.5"),
        total_sell_quantity=Decimal("888.5"),
        open_price=Decimal("805.00"),
        high_price=Decimal("815.10"),
        low_price=Decimal("801.20"),
        close_price=Decimal("803.00"),
        raw_payload={"exchange_type": 1, "token": "3045"},
    )


def test_recorder_round_trips_tick_without_floats(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'market.db'}"

    with SQLiteMarketRecorder(database_url) as recorder:
        assert recorder.record_tick(_tick()) is True
        assert recorder.record_tick(_tick()) is False
        assert recorder.count_ticks() == 1

        loaded = recorder.load_ticks(exchange=Exchange.NSE_CM, symbol_token="3045")

    assert loaded == [_tick()]
    assert loaded[0].last_traded_price == Decimal("812.25")
    assert isinstance(loaded[0].last_traded_price, Decimal)


def test_recorder_records_feed_audit_events() -> None:
    event = FeedEvent(
        event_type=FeedEventType.RECONNECTING,
        occurred_at=datetime(2026, 8, 10, 9, 31, tzinfo=IST),
        message="Reconnecting market feed",
        details={"attempt": 2, "delay_seconds": 2.0},
    )

    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        row_id = recorder.record_feed_event(event)
        loaded = recorder.load_feed_events()

    assert row_id == 1
    assert loaded == [event]


def test_batch_insert_is_idempotent() -> None:
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        inserted = recorder.record_ticks([_tick(1), _tick(2), _tick(2)])

        assert inserted == 2
        assert recorder.count_ticks() == 2
