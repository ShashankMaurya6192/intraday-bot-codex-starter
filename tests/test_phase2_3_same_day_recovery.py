from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from trading_bot.broker import BrokerHistoricalRateLimitError, BrokerInstrument
from trading_bot.config import Settings
from trading_bot.domain import (
    IST,
    Exchange,
    HistoricalProvider,
    MarketTick,
    SubscriptionMode,
    TradingMode,
)
from trading_bot.marketdata import (
    Candle,
    CandleInterval,
    HistoricalBootstrapError,
    HistoricalBootstrapService,
    HistoricalProviderDataset,
    MarketDataProvenance,
    MarketReplayEngine,
    SessionReference,
    YFinanceHistoricalProvider,
    normalize_yfinance_frames,
    yfinance_date_chunks,
    yfinance_symbol,
)
from trading_bot.marketdata.recovery import session_reference_from_tick
from trading_bot.scanner import MarketScanner
from trading_bot.storage import SQLiteMarketRecorder

SESSION = date(2026, 8, 10)


def test_history_provider_configuration_is_angel_and_opt_out_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.trading_mode is TradingMode.PAPER
    assert settings.historical_provider is HistoricalProvider.ANGEL
    assert settings.historical_fallback_enabled is False


def _tick(
    token: str,
    at: time,
    *,
    sequence: int = 1,
    symbol: str = "SBIN-EQ",
    open_price: str = "1071",
    high_price: str = "1071",
    low_price: str = "1060.4",
    ltp: str = "1064.3",
    average: str | None = "1064.71",
    volume: int | None = 613_366,
    received_delay_ms: int = 500,
) -> MarketTick:
    exchange_at = datetime.combine(SESSION, at, tzinfo=IST)
    return MarketTick(
        exchange=Exchange.NSE_CM,
        symbol_token=token,
        trading_symbol=symbol,
        subscription_mode=SubscriptionMode.QUOTE,
        sequence_number=sequence,
        exchange_timestamp=exchange_at,
        received_at=exchange_at + timedelta(milliseconds=received_delay_ms),
        last_traded_price=Decimal(ltp),
        average_traded_price=(Decimal(average) if average is not None else None),
        volume_trade_for_the_day=volume,
        open_price=Decimal(open_price),
        high_price=Decimal(high_price),
        low_price=Decimal(low_price),
    )


def _evaluate(at: time) -> datetime:
    return datetime.combine(SESSION, at, tzinfo=IST)


def test_late_start_recovers_first_five_only_after_0920_without_fake_minutes() -> None:
    tick = _tick("3045", time(9, 19, 59))
    engine = MarketReplayEngine(stale_after_seconds=5)

    before = engine.replay([tick], evaluation_at=_evaluate(time(9, 19, 59, 400_000)))
    received = engine.replay(
        [tick], evaluation_at=_evaluate(time(9, 19, 59, 600_000))
    )
    after = engine.replay([tick], evaluation_at=_evaluate(time(9, 20)))

    assert before.session_references == ()
    assert len(received.session_references) == 1
    assert not [
        item for item in before.candles if item.interval is CandleInterval.FIVE_MINUTES
    ]
    five = [
        item for item in after.candles if item.interval is CandleInterval.FIVE_MINUTES
    ]
    assert len(five) == 1
    assert five[0].start_at == _evaluate(time(9, 15))
    assert five[0].open_price == Decimal("1071")
    assert five[0].high_price == Decimal("1071")
    assert five[0].low_price == Decimal("1060.4")
    assert five[0].close_price == Decimal("1064.3")
    assert five[0].volume == 613_366
    assert five[0].provenance is MarketDataProvenance.ANGEL_SESSION_FIELDS
    one = [item for item in after.candles if item.interval is CandleInterval.ONE_MINUTE]
    assert [item.start_at.time() for item in one] == [time(9, 19)]


def test_delayed_stale_receipt_cannot_be_used_for_recovery() -> None:
    tick = _tick("3045", time(9, 19, 59), received_delay_ms=61_000)
    engine = MarketReplayEngine(stale_after_seconds=5)

    hidden = engine.replay([tick], evaluation_at=_evaluate(time(9, 20)))
    stale = engine.replay([tick], evaluation_at=_evaluate(time(9, 21, 1)))

    assert hidden.session_references == ()
    assert hidden.candles == ()
    assert stale.session_references == ()
    assert not any(
        item.provenance is MarketDataProvenance.ANGEL_SESSION_FIELDS
        for item in stale.candles
    )


def test_malformed_session_fields_do_not_create_a_reference_or_opening_candle() -> None:
    malformed = _tick(
        "3045",
        time(9, 19, 59),
        open_price="1071",
        high_price="1065",
        low_price="1060",
        ltp="1064",
    )
    result = MarketReplayEngine(stale_after_seconds=5).replay(
        [malformed], evaluation_at=_evaluate(time(9, 20))
    )

    assert result.session_references == ()
    assert not [
        item
        for item in result.candles
        if item.interval is CandleInterval.FIVE_MINUTES
    ]


@pytest.mark.parametrize("observed_at", [time(9, 19, 57), time(9, 20)])
def test_stale_or_post_boundary_snapshot_does_not_recover_first_five(
    observed_at: time,
) -> None:
    result = MarketReplayEngine(stale_after_seconds=10).replay(
        [_tick("3045", observed_at)],
        evaluation_at=_evaluate(time(9, 20, 1)),
    )
    assert not [
        item
        for item in result.candles
        if item.interval is CandleInterval.FIVE_MINUTES
        and item.start_at.time() == time(9, 15)
    ]


def test_opening_range_snapshot_is_invisible_before_0930_and_visible_after() -> None:
    tick = _tick(
        "3045",
        time(9, 29, 59),
        high_price="1078",
        low_price="1059",
        ltp="1070",
    )
    engine = MarketReplayEngine(stale_after_seconds=5)

    before = engine.replay([tick], evaluation_at=_evaluate(time(9, 29, 59, 900_000)))
    after = engine.replay([tick], evaluation_at=_evaluate(time(9, 30)))

    assert before.opening_range_references == ()
    assert len(after.opening_range_references) == 1
    reference = after.opening_range_references[0]
    assert reference.session_open == Decimal("1071")
    assert reference.session_high == Decimal("1078")
    assert reference.session_low == Decimal("1059")


def test_nifty_zero_atp_is_not_exposed_as_a_vwap_reference() -> None:
    reference = session_reference_from_tick(
        _tick(
            "99926000",
            time(9, 29, 59),
            symbol="NIFTY 50",
            open_price="24575.1",
            high_price="24576.85",
            low_price="24500.85",
            ltp="24503.95",
            average="0",
            volume=0,
        ),
        trusted=True,
    )

    assert reference is not None
    assert reference.average_traded_price is None
    assert reference.cumulative_volume == 0


class _Baselines:
    def __init__(self) -> None:
        self.minute_offset: int | None = None

    def load_rvol_baselines(
        self,
        *,
        exchange: Exchange,
        symbol_token: str,
        before_date: date,
        minute_offset: int,
        lookback_sessions: int,
    ) -> tuple[int, ...]:
        self.minute_offset = minute_offset
        return (100,) * 10


def _five(index: int) -> Candle:
    start = datetime.combine(SESSION - timedelta(days=1), time(9, 15), IST)
    start += timedelta(minutes=index * 5)
    price = Decimal("100") + Decimal(index) / Decimal(10)
    return Candle(
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        interval=CandleInterval.FIVE_MINUTES,
        start_at=start,
        end_at=start + timedelta(minutes=5),
        open_price=price,
        high_price=price + 1,
        low_price=price - 1,
        close_price=price + Decimal("0.2"),
        volume=100,
        tick_count=1,
        completed=True,
        trusted=True,
    )


def test_recovered_open_atp_and_cumulative_volume_drive_scanner_indicators() -> None:
    reference = SessionReference(
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        session_date=SESSION,
        observed_at=_evaluate(time(9, 29, 59)),
        session_open=Decimal("100"),
        session_high=Decimal("111"),
        session_low=Decimal("99"),
        last_traded_price=Decimal("110"),
        average_traded_price=Decimal("105"),
        cumulative_volume=200,
        provenance=MarketDataProvenance.ANGEL_SESSION_FIELDS,
        trusted=True,
    )
    baselines = _Baselines()
    values = MarketScanner()._indicator_values(
        instrument=BrokerInstrument(
            Exchange.NSE_CM, "3045", "SBIN-EQ", Decimal("0.05"), 1, "EQ"
        ),
        one_minute=[],
        rolling_five_minute=[_five(index) for index in range(20)],
        benchmark_open=Decimal("100"),
        benchmark_current=Decimal("105"),
        baseline_store=baselines,
        session_date=SESSION,
        now=_evaluate(time(9, 30)),
        session_reference=reference,
        opening_range_reference=reference,
    )

    assert isinstance(values, dict)
    assert values["vwap"] == Decimal("105")
    assert values["relative_volume"] == Decimal("2")
    assert values["relative_strength"] == Decimal("5")
    assert values["opening_high"] == Decimal("111")
    assert baselines.minute_offset == 14


def _instrument(
    token: str = "3045", symbol: str = "SBIN-EQ", kind: str = "EQ"
) -> BrokerInstrument:
    return BrokerInstrument(
        exchange=Exchange.NSE_CM,
        symbol_token=token,
        trading_symbol=symbol,
        tick_size=Decimal("0") if kind == "AMXIDX" else Decimal("0.05"),
        lot_size=1,
        instrument_type=kind,
    )


def _session_frame(session_date: date, *, minutes: int = 375) -> pd.DataFrame:
    index = pd.date_range(
        datetime.combine(session_date, time(9, 15), tzinfo=IST),
        periods=minutes,
        freq="min",
    )
    return pd.DataFrame(
        {
            "Open": [100.0] * minutes,
            "High": [101.0] * minutes,
            "Low": [99.0] * minutes,
            "Close": [100.5] * minutes,
            "Volume": list(range(minutes)),
        },
        index=index,
    )


def test_yfinance_mapping_and_seven_day_chunks_are_deterministic() -> None:
    assert yfinance_symbol(_instrument()) == "SBIN.NS"
    assert yfinance_symbol(_instrument("99926000", "NIFTY 50", "AMXIDX")) == "^NSEI"
    chunks = yfinance_date_chunks(date(2026, 7, 1), date(2026, 7, 15))
    assert chunks == (
        (date(2026, 7, 1), date(2026, 7, 8)),
        (date(2026, 7, 8), date(2026, 7, 15)),
        (date(2026, 7, 15), date(2026, 7, 16)),
    )


def test_yfinance_normalization_discards_partial_sessions_and_normalizes_tz() -> None:
    complete_date = date(2026, 8, 7)
    partial_date = date(2026, 8, 6)
    utc_complete = _session_frame(complete_date)
    utc_complete.index = utc_complete.index.tz_convert("UTC")
    candles, sessions, discarded, rows = normalize_yfinance_frames(
        (utc_complete, _session_frame(partial_date, minutes=30)),
        instrument=_instrument(),
        through_date=complete_date,
        minimum_sessions=1,
    )

    assert sessions == (complete_date,)
    assert discarded == (partial_date,)
    assert rows == 405
    assert len(candles) == 375
    assert candles[0].start_at.tzinfo == IST
    assert candles[0].provenance is MarketDataProvenance.YFINANCE


def test_yfinance_insufficient_or_malformed_history_fails_closed() -> None:
    with pytest.raises(HistoricalBootstrapError, match="insufficient"):
        normalize_yfinance_frames(
            (_session_frame(date(2026, 8, 7), minutes=30),),
            instrument=_instrument(),
            through_date=date(2026, 8, 7),
            minimum_sessions=1,
        )
    malformed = _session_frame(date(2026, 8, 7))
    malformed.iloc[0, malformed.columns.get_loc("High")] = float("nan")
    with pytest.raises(HistoricalBootstrapError, match="malformed"):
        normalize_yfinance_frames(
            (malformed,),
            instrument=_instrument(),
            through_date=date(2026, 8, 7),
            minimum_sessions=1,
        )


class _ThrottleBroker:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_historical_candles(self, parameters):
        self.calls += 1
        raise BrokerHistoricalRateLimitError("rate limit")


class _Fallback:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(
        self,
        *,
        instrument: BrokerInstrument,
        from_date: date,
        through_date: date,
        minimum_sessions: int,
    ) -> HistoricalProviderDataset:
        self.calls.append(instrument.trading_symbol)
        frame = _session_frame(through_date)
        candles, sessions, _, _ = normalize_yfinance_frames(
            (frame,),
            instrument=instrument,
            through_date=through_date,
            minimum_sessions=1,
        )
        return HistoricalProviderDataset(
            provider=HistoricalProvider.YFINANCE,
            provenance=MarketDataProvenance.YFINANCE,
            one_minute_candles=candles,
            complete_session_dates=sessions,
            source_sha256="a" * 64,
            requested_chunks=1,
            raw_rows=375,
        )


def test_auto_throttle_stops_angel_before_explicit_paper_fallback(tmp_path) -> None:
    broker = _ThrottleBroker()
    fallback = _Fallback()
    with SQLiteMarketRecorder(f"sqlite:///{tmp_path / 'history.db'}") as store:
        result = HistoricalBootstrapService(
            broker,
            store,
            lookback_days=22,
            minimum_sessions=1,
            request_delay_seconds=0,
            max_attempts=1,
            provider=HistoricalProvider.AUTO,
            fallback_enabled=True,
            fallback_provider=fallback,
            trading_mode=TradingMode.PAPER,
        ).bootstrap(
            (_instrument(), _instrument("1333", "HDFCBANK-EQ")),
            benchmark=_instrument("99926000", "NIFTY 50", "AMXIDX"),
            now=datetime(2026, 8, 11, 8, tzinfo=IST),
            force=True,
        )
        persisted = [
            store.load_historical_bootstrap_status(
                exchange=Exchange.NSE_CM,
                symbol_token=token,
            )
            for token in ("3045", "1333", "99926000")
        ]

    assert broker.calls == 1
    assert len(fallback.calls) == 3
    assert result.status == "COMPLETED_WITH_FALLBACK"
    assert result.fallback_used
    assert result.rate_limited_symbol is not None
    assert all(item.provider == "yfinance" for item in result.symbols)
    assert all(item is not None for item in persisted)
    assert all(item.provider == "yfinance" for item in persisted if item is not None)
    assert all(
        item.provenance is MarketDataProvenance.YFINANCE
        for item in persisted
        if item is not None
    )


def test_yfinance_bootstrap_requires_opt_in_and_never_allows_live() -> None:
    with SQLiteMarketRecorder("sqlite:///:memory:") as store:
        with pytest.raises(ValueError, match="explicit fallback opt-in"):
            HistoricalBootstrapService(
                None,
                store,
                provider=HistoricalProvider.YFINANCE,
                fallback_enabled=False,
                fallback_provider=_Fallback(),
            )
        with pytest.raises(ValueError, match="PAPER-only"):
            HistoricalBootstrapService(
                None,
                store,
                provider=HistoricalProvider.YFINANCE,
                fallback_enabled=True,
                fallback_provider=_Fallback(),
                trading_mode=TradingMode.LIVE,
            )


def test_yfinance_downloader_receives_required_safe_parameters() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def downloader(symbol: str, **kwargs):
        calls.append((symbol, kwargs))
        return _session_frame(date.fromisoformat(str(kwargs["start"])))

    dataset = YFinanceHistoricalProvider(downloader=downloader).fetch(
        instrument=_instrument(),
        from_date=date(2026, 8, 3),
        through_date=date(2026, 8, 7),
        minimum_sessions=1,
    )

    assert len(calls) == 1
    assert calls[0][0] == "SBIN.NS"
    assert calls[0][1]["interval"] == "1m"
    assert calls[0][1]["auto_adjust"] is False
    assert calls[0][1]["prepost"] is False
    assert dataset.provider is HistoricalProvider.YFINANCE


class _MalformedFallback:
    def fetch(self, **kwargs) -> HistoricalProviderDataset:
        raise HistoricalBootstrapError("yfinance candle row is malformed")


def test_malformed_fallback_never_replaces_previous_valid_dataset(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'atomic.db'}"
    instrument = _instrument()
    benchmark = _instrument("99926000", "NIFTY 50", "AMXIDX")
    now = datetime(2026, 8, 11, 8, tzinfo=IST)
    with SQLiteMarketRecorder(database_url) as store:
        first = HistoricalBootstrapService(
            None,
            store,
            lookback_days=22,
            minimum_sessions=1,
            provider=HistoricalProvider.YFINANCE,
            fallback_enabled=True,
            fallback_provider=_Fallback(),
        ).bootstrap((instrument,), benchmark=benchmark, now=now, force=True)
        before = store.load_historical_bootstrap_status(
            exchange=instrument.exchange,
            symbol_token=instrument.symbol_token,
        )
        second = HistoricalBootstrapService(
            None,
            store,
            lookback_days=22,
            minimum_sessions=1,
            provider=HistoricalProvider.YFINANCE,
            fallback_enabled=True,
            fallback_provider=_MalformedFallback(),
        ).bootstrap((instrument,), benchmark=benchmark, now=now, force=True)
        after = store.load_historical_bootstrap_status(
            exchange=instrument.exchange,
            symbol_token=instrument.symbol_token,
        )

    assert first.failed_symbols == 0
    assert second.failed_symbols == 2
    assert before is not None and after is not None
    assert after.source_sha256 == before.source_sha256
    assert after.fetched_at == before.fetched_at


def test_sqlite_v4_migration_marks_existing_angel_history_provenance(tmp_path) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE market_candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange TEXT NOT NULL,
                symbol_token TEXT NOT NULL,
                trading_symbol TEXT,
                interval_minutes INTEGER NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                open_price TEXT NOT NULL,
                high_price TEXT NOT NULL,
                low_price TEXT NOT NULL,
                close_price TEXT NOT NULL,
                volume INTEGER NOT NULL,
                tick_count INTEGER NOT NULL,
                completed INTEGER NOT NULL,
                trusted INTEGER NOT NULL,
                UNIQUE (exchange, symbol_token, interval_minutes, start_at)
            );
            CREATE TABLE historical_bootstrap_status (
                exchange TEXT NOT NULL,
                symbol_token TEXT NOT NULL,
                trading_symbol TEXT NOT NULL,
                from_date TEXT NOT NULL,
                through_date TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                sessions_found INTEGER NOT NULL,
                one_minute_candles INTEGER NOT NULL,
                five_minute_candles INTEGER NOT NULL,
                source_sha256 TEXT NOT NULL,
                PRIMARY KEY (exchange, symbol_token)
            );
            INSERT INTO market_candles VALUES (
                NULL, 'NSE_CM', '3045', 'SBIN-EQ', 1,
                '2026-08-07T09:15:00+05:30', '2026-08-07T09:16:00+05:30',
                '100', '101', '99', '100.5', 10, 1, 1, 1
            );
            INSERT INTO historical_bootstrap_status VALUES (
                'NSE_CM', '3045', 'SBIN-EQ', '2026-08-07', '2026-08-07',
                '2026-08-08T08:00:00+05:30', 1, 375, 75,
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            );
            """
        )

    with SQLiteMarketRecorder(f"sqlite:///{database_path}") as store:
        candles = store.load_candles(
            exchange=Exchange.NSE_CM,
            symbol_token="3045",
        )
        status = store.load_historical_bootstrap_status(
            exchange=Exchange.NSE_CM,
            symbol_token="3045",
        )
    with sqlite3.connect(database_path) as connection:
        versions = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_version")
        }

    assert candles[0].provenance is MarketDataProvenance.ANGEL_HISTORICAL
    assert status is not None
    assert status.provider == "angel"
    assert status.provenance is MarketDataProvenance.ANGEL_HISTORICAL
    assert 4 in versions
