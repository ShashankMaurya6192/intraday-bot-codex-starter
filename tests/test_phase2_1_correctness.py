from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.broker import (
    AngelInstrumentMasterCache,
    AngelIntradayAllowedCache,
    BrokerInstrument,
    IntradayUniverseError,
)
from trading_bot.domain import IST, Exchange, MarketTick, SubscriptionMode
from trading_bot.marketdata import (
    Candle,
    CandleInterval,
    HistoricalBootstrapError,
    HistoricalBootstrapService,
    InstrumentHealth,
    MarketDataHealthStatus,
    MarketRegime,
    MarketReplayEngine,
    ReplaySafetyLimitExceeded,
    classify_market_regime,
    normalize_historical_response,
)
from trading_bot.reference_data import Nifty500Cache
from trading_bot.reference_data.nifty500 import NIFTY500_CONSTITUENTS_URL
from trading_bot.reference_data.universe import (
    ScannerSubscriptionError,
    build_scanner_subscription_plan,
)
from trading_bot.storage import SQLiteMarketRecorder

BASE = datetime(2026, 8, 10, 9, 15, tzinfo=IST)


def _tick(
    sequence: int,
    minute: int,
    *,
    price: str | None = None,
    received_at: datetime | None = None,
) -> MarketTick:
    exchange_at = BASE + timedelta(minutes=minute)
    return MarketTick(
        exchange=Exchange.NSE_CM,
        symbol_token="1",
        trading_symbol="ABC-EQ",
        subscription_mode=SubscriptionMode.QUOTE,
        sequence_number=sequence,
        exchange_timestamp=exchange_at,
        received_at=received_at or exchange_at + timedelta(milliseconds=100),
        last_traded_price=Decimal(price or str(100 + minute)),
        volume_trade_for_the_day=100 + minute * 10,
    )


@pytest.mark.parametrize(
    ("evaluation_at", "expected_five_minute"),
    [
        (BASE + timedelta(minutes=2), 0),
        (BASE + timedelta(minutes=4, seconds=59), 0),
        (BASE + timedelta(minutes=5), 1),
    ],
)
def test_replay_as_of_never_emits_partial_five_minute_candle(
    evaluation_at: datetime, expected_five_minute: int
) -> None:
    ticks = [_tick(index + 1, index) for index in range(5)]
    result = MarketReplayEngine(stale_after_seconds=5).replay(
        ticks, evaluation_at=evaluation_at
    )

    five_minute = [
        candle
        for candle in result.candles
        if candle.interval is CandleInterval.FIVE_MINUTES
    ]
    assert len(five_minute) == expected_five_minute
    assert all(candle.end_at <= evaluation_at for candle in result.candles)


def test_replay_as_of_ignores_future_receipt_and_epoch_and_is_deterministic() -> None:
    evaluation_at = BASE + timedelta(minutes=5)
    future_receipt = _tick(
        99,
        4,
        price="999",
        received_at=BASE + timedelta(minutes=6),
    )
    engine = MarketReplayEngine(stale_after_seconds=5)
    kwargs = {
        "evaluation_at": evaluation_at,
        "feed_epoch_starts": (BASE + timedelta(minutes=6),),
    }

    ticks = [*[_tick(index + 1, index) for index in range(5)], future_receipt]
    first = engine.replay(ticks, **kwargs)
    second = engine.replay(ticks, **kwargs)

    assert first == second
    assert first.ignored_future_ticks == 1
    assert first.raw_ticks_replayed == 5
    assert first.health[0].feed_epoch == 0
    five_minute = next(
        candle
        for candle in first.candles
        if candle.interval is CandleInterval.FIVE_MINUTES
    )
    assert five_minute.high_price != Decimal("999")


def test_full_historical_replay_flushes_through_normal_session_close() -> None:
    result = MarketReplayEngine(stale_after_seconds=5).replay(
        [_tick(1, 0), _tick(2, 374)]
    )

    assert any(
        candle.end_at == BASE.replace(hour=15, minute=30)
        for candle in result.candles
    )


def _benchmark_candles(step: str) -> list[Candle]:
    result = []
    for index in range(20):
        close = Decimal("100") + Decimal(step) * index
        start_at = BASE + timedelta(minutes=5 * index)
        result.append(
            Candle(
                exchange=Exchange.NSE_CM,
                symbol_token="99926000",
                trading_symbol="NIFTY 50",
                interval=CandleInterval.FIVE_MINUTES,
                start_at=start_at,
                end_at=start_at + timedelta(minutes=5),
                open_price=close,
                high_price=close + Decimal("0.05"),
                low_price=close - Decimal("0.05"),
                close_price=close,
                volume=0,
                tick_count=1,
                completed=True,
                trusted=True,
            )
        )
    return result


def _health(
    status: MarketDataHealthStatus = MarketDataHealthStatus.READY,
) -> InstrumentHealth:
    return InstrumentHealth(
        exchange=Exchange.NSE_CM,
        symbol_token="99926000",
        feed_epoch=0,
        status=status,
        latest_exchange_timestamp=BASE + timedelta(minutes=100),
        latest_received_timestamp=BASE + timedelta(minutes=100),
        sequence_number=20,
        data_age_seconds=0,
        duplicate_count=0,
        out_of_order_count=0,
        sequence_gap_count=0,
        last_health_transition=BASE,
    )


def test_zero_volume_nifty_regime_has_all_deterministic_states() -> None:
    up = _benchmark_candles("0.1")
    down = _benchmark_candles("-0.1")
    flat = _benchmark_candles("0")

    assert classify_market_regime(up, _health()) is MarketRegime.TREND_UP
    assert classify_market_regime(down, _health()) is MarketRegime.TREND_DOWN
    assert classify_market_regime(flat, _health()) is MarketRegime.RANGE
    assert (
        classify_market_regime(up, _health(MarketDataHealthStatus.STALE))
        is MarketRegime.UNKNOWN
    )
    assert classify_market_regime(up[:19], _health()) is MarketRegime.UNKNOWN
    assert classify_market_regime(up, _health()) is MarketRegime.TREND_UP


def _instrument_bytes() -> bytes:
    return (
        b'[{"token":"1","symbol":"ABC-EQ","tick_size":"5",'
        b'"lotsize":"1","instrumenttype":"","exch_seg":"NSE"},'
        b'{"token":"2","symbol":"XYZ-EQ","tick_size":"5",'
        b'"lotsize":"1","instrumenttype":"","exch_seg":"NSE"},'
        b'{"token":"99926000","symbol":"Nifty 50","tick_size":"0",'
        b'"lotsize":"1","instrumenttype":"AMXIDX","exch_seg":"NSE"}]'
    )


def _nifty_csv() -> bytes:
    return (
        b"Company Name,Industry,Symbol,Series,ISIN Code\n"
        b"ABC Limited,Test,ABC,EQ,INE000000001\n"
        b"XYZ Limited,Test,XYZ,EQ,INE000000002\n"
    )


def test_automatic_scanner_plan_includes_benchmark_once_and_classifies_exclusions(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 10, 8, 30, tzinfo=IST)
    instruments = AngelInstrumentMasterCache(
        tmp_path / "instruments.json", max_age_seconds=3600
    )
    instruments.refresh(source_bytes=_instrument_bytes(), fetched_at=now)
    nifty = Nifty500Cache(tmp_path / "nifty.json", max_age_seconds=3600)
    nifty.refresh(source="fixture.csv", source_bytes=_nifty_csv(), fetched_at=now)
    intraday = AngelIntradayAllowedCache(
        tmp_path / "intraday.json", max_age_seconds=3600
    )
    intraday.refresh_from_response(
        {
            "status": True,
            "data": [{"exchange": "NSE", "symbol": "ABC", "multiplier": "5"}],
        },
        fetched_at=now,
    )

    plan = build_scanner_subscription_plan(
        nifty,
        instruments,
        intraday,
        benchmark_trading_symbol="NIFTY 50",
    )

    assert plan.requested_count == 2
    assert plan.eligible_count == 1
    assert plan.expected_ineligible_count == 1
    assert plan.mapping_failure_count == 0
    assert plan.benchmark_count == 1
    assert [item.symbol_token for item in plan.subscriptions].count("99926000") == 1
    assert len(plan.subscriptions) == 2
    with pytest.raises(ScannerSubscriptionError, match="1000-token"):
        build_scanner_subscription_plan(
            nifty,
            instruments,
            intraday,
            benchmark_trading_symbol="NIFTY 50",
            maximum_subscriptions=1,
        )


def test_intraday_parser_casing_and_malformed_rows_fail_closed(tmp_path) -> None:
    cache = AngelIntradayAllowedCache(tmp_path / "intraday.json", max_age_seconds=3600)
    now = datetime(2026, 8, 10, 8, 30, tzinfo=IST)
    assert (
        cache.refresh_from_response(
            {
                "status": True,
                "data": [
                    {"exchange": "NSE", "symbol": "ABC", "multiplier": "5"},
                    {"Exchange": "NSE", "SymbolName": "XYZ", "Multiplier": "4"},
                ],
            },
            fetched_at=now,
        )
        == 2
    )
    original = cache.cache_path.read_bytes()

    invalid_rows = [
        {"symbol": "ABC", "multiplier": "5"},
        {"exchange": "BSE", "symbol": "ABC", "multiplier": "5"},
        {"exchange": "NSE", "multiplier": "5"},
        {"exchange": "NSE", "symbol": "ABC"},
        {"exchange": "NSE", "symbol": "ABC", "multiplier": "0"},
    ]
    for row in invalid_rows:
        with pytest.raises(IntradayUniverseError):
            cache.refresh_from_response(
                {"status": True, "data": [row]}, fetched_at=now
            )
        assert cache.cache_path.read_bytes() == original
    with pytest.raises(IntradayUniverseError, match="duplicate"):
        cache.refresh_from_response(
            {
                "status": True,
                "data": [
                    {"exchange": "NSE", "symbol": "ABC", "multiplier": "5"},
                    {"exchange": "NSE", "symbol": "ABC", "multiplier": "5"},
                ],
            },
            fetched_at=now,
        )


def _historical_response(
    session_date: date, *, start: str, volume: int
) -> dict[str, object]:
    rows: list[list[object]] = []
    base_price = Decimal(start)
    session_open = datetime.combine(
        session_date, datetime.min.time(), tzinfo=IST
    ).replace(hour=9, minute=15)
    for minute in range(375):
        close = base_price + Decimal(minute) / Decimal("100")
        rows.append(
            [
                (session_open + timedelta(minutes=minute)).isoformat(),
                str(close),
                str(close + Decimal("0.05")),
                str(close - Decimal("0.05")),
                str(close),
                volume,
            ]
        )
    return {"status": True, "message": "SUCCESS", "data": rows}


class _HistoricalBroker:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch_historical_candles(self, parameters):
        token = str(parameters["symboltoken"])
        self.calls.append(token)
        return self.responses[token]


def test_historical_bootstrap_is_atomic_idempotent_and_excludes_index_rvol() -> None:
    session_date = date(2026, 8, 7)
    equity = BrokerInstrument(Exchange.NSE_CM, "1", "ABC-EQ", Decimal("0.05"))
    benchmark = BrokerInstrument(
        Exchange.NSE_CM,
        "99926000",
        "NIFTY 50",
        Decimal("0"),
        instrument_type="AMXIDX",
    )
    broker = _HistoricalBroker(
        {
            "1": _historical_response(session_date, start="100", volume=10),
            "99926000": _historical_response(
                session_date, start="20000", volume=0
            ),
        }
    )
    now = datetime(2026, 8, 10, 8, 0, tzinfo=IST)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        service = HistoricalBootstrapService(
            broker,
            recorder,
            lookback_days=22,
            minimum_sessions=1,
            request_delay_seconds=0,
            max_attempts=2,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
            sleeper=lambda _delay: None,
        )
        first = service.bootstrap((equity,), benchmark=benchmark, now=now)
        second = service.bootstrap((equity,), benchmark=benchmark, now=now)

        assert first.completed_symbols == 2
        assert first.failed_symbols == 0
        assert second.completed_symbols == 2
        assert broker.calls == ["1", "99926000"]
        assert len(recorder.load_candles(symbol_token="1")) == 450
        assert len(recorder.load_candles(symbol_token="99926000")) == 450
        assert recorder.load_rvol_baselines(
            exchange=Exchange.NSE_CM,
            symbol_token="1",
            before_date=date(2026, 8, 10),
            minute_offset=374,
            lookback_sessions=15,
        ) == (3750,)
        assert not recorder.load_rvol_baselines(
            exchange=Exchange.NSE_CM,
            symbol_token="99926000",
            before_date=date(2026, 8, 10),
            minute_offset=374,
            lookback_sessions=15,
        )

        previous = recorder.load_historical_bootstrap_status(
            exchange=Exchange.NSE_CM, symbol_token="1"
        )
        malformed = _historical_response(session_date, start="100", volume=10)
        malformed["data"] = list(malformed["data"])[:-1]
        broker.responses = {"1": malformed, "99926000": malformed}
        failed = service.bootstrap((equity,), benchmark=benchmark, now=now, force=True)
        assert failed.failed_symbols == 2
        assert recorder.load_historical_bootstrap_status(
            exchange=Exchange.NSE_CM, symbol_token="1"
        ) == previous
        assert len(recorder.load_candles(symbol_token="1")) == 450


def test_historical_partial_response_and_replay_hard_limit_fail_closed() -> None:
    instrument = BrokerInstrument(
        Exchange.NSE_CM, "1", "ABC-EQ", Decimal("0.05")
    )
    partial = _historical_response(date(2026, 8, 7), start="100", volume=10)
    partial["data"] = list(partial["data"])[:-1]
    with pytest.raises(HistoricalBootstrapError, match="partial session"):
        normalize_historical_response(
            partial, instrument=instrument, minimum_sessions=1
        )

    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_ticks([_tick(1, 0), _tick(2, 1)])
        with pytest.raises(ReplaySafetyLimitExceeded):
            MarketReplayEngine(stale_after_seconds=5).replay_date(
                recorder,
                BASE.date(),
                evaluation_at=BASE + timedelta(minutes=2),
                persist=False,
                hard_tick_limit=1,
            )


def test_historical_bootstrap_retries_with_bounded_exponential_delay() -> None:
    benchmark = BrokerInstrument(
        Exchange.NSE_CM,
        "99926000",
        "NIFTY 50",
        Decimal("0"),
        instrument_type="AMXIDX",
    )

    class FlakyBroker:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_historical_candles(self, _parameters):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary broker failure")
            return _historical_response(date(2026, 8, 7), start="20000", volume=0)

    broker = FlakyBroker()
    delays: list[float] = []
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        result = HistoricalBootstrapService(
            broker,
            recorder,
            lookback_days=22,
            minimum_sessions=1,
            request_delay_seconds=0,
            max_attempts=2,
            retry_initial_delay_seconds=1,
            retry_max_delay_seconds=2,
            sleeper=delays.append,
        ).bootstrap(
            (),
            benchmark=benchmark,
            now=datetime(2026, 8, 10, 8, 0, tzinfo=IST),
        )

    assert result.completed_symbols == 1
    assert broker.calls == 2
    assert delays == [1]


def test_nifty_source_is_current_official_nse_archive() -> None:
    assert NIFTY500_CONSTITUENTS_URL == (
        "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
    )
