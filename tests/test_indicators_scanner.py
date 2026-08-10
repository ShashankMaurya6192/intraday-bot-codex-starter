from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from trading_bot.broker import BrokerInstrument
from trading_bot.domain import IST, Exchange
from trading_bot.indicators import (
    IndicatorStatus,
    atr,
    ema,
    opening_range,
    relative_strength,
    relative_volume,
    session_vwap,
    vwap_distance,
)
from trading_bot.marketdata import (
    Candle,
    CandleInterval,
    InstrumentHealth,
    MarketDataHealthStatus,
    MarketRegime,
    classify_market_regime,
)
from trading_bot.scanner import MarketScanner, ScannerReadiness, ScannerWeights

BASE = datetime(2026, 8, 10, 9, 15, tzinfo=IST)


def _candle(
    index: int,
    *,
    interval: CandleInterval = CandleInterval.ONE_MINUTE,
    start_price: Decimal = Decimal("100"),
    step: Decimal = Decimal("1"),
    token: str = "1",
    symbol: str = "ABC-EQ",
    volume: int = 10,
    trusted: bool = True,
) -> Candle:
    close = start_price + step * index
    start = BASE + timedelta(minutes=int(interval) * index)
    return Candle(
        exchange=Exchange.NSE_CM,
        symbol_token=token,
        trading_symbol=symbol,
        interval=interval,
        start_at=start,
        end_at=start + timedelta(minutes=int(interval)),
        open_price=close,
        high_price=close + Decimal("0.05"),
        low_price=close - Decimal("0.05"),
        close_price=close,
        volume=volume,
        tick_count=1,
        completed=True,
        trusted=trusted,
    )


def _health(
    token: str,
    *,
    status: MarketDataHealthStatus = MarketDataHealthStatus.READY,
) -> InstrumentHealth:
    at = BASE + timedelta(minutes=20)
    return InstrumentHealth(
        exchange=Exchange.NSE_CM,
        symbol_token=token,
        feed_epoch=0,
        status=status,
        latest_exchange_timestamp=at,
        latest_received_timestamp=at,
        sequence_number=20,
        data_age_seconds=0.0,
        duplicate_count=0,
        out_of_order_count=0,
        sequence_gap_count=0,
        last_health_transition=at,
    )


def test_vwap_ema9_ema20_atr14_and_distances() -> None:
    candle = Candle(
        exchange=Exchange.NSE_CM,
        symbol_token="1",
        trading_symbol="ABC-EQ",
        interval=CandleInterval.ONE_MINUTE,
        start_at=BASE,
        end_at=BASE + timedelta(minutes=1),
        open_price=Decimal("10"),
        high_price=Decimal("12"),
        low_price=Decimal("8"),
        close_price=Decimal("10"),
        volume=100,
        tick_count=1,
        completed=True,
        trusted=True,
    )
    assert session_vwap([candle]).value == Decimal("10")
    values = [Decimal(index) for index in range(1, 21)]
    assert ema(values, 9).value == Decimal("16")
    assert ema(values, 20).value == Decimal("10.5")
    assert ema(values[:8], 9).status is IndicatorStatus.NOT_READY

    candles = [
        _candle(
            index,
            interval=CandleInterval.FIVE_MINUTES,
            start_price=Decimal("100"),
            step=Decimal("1"),
        )
        for index in range(15)
    ]
    # The helper's high/low span is 0.10 and gaps are 0.95, so TR is 1.05.
    assert atr(candles, 14).value == Decimal("1.05")
    distance = vwap_distance(
        price=Decimal("105"), vwap=Decimal("100"), atr_value=Decimal("2.5")
    )
    assert distance.value is not None
    assert distance.value.percentage == Decimal("5.00")
    assert distance.value.atr_units == Decimal("2")


def test_rvol_relative_strength_and_opening_range_are_like_for_like() -> None:
    assert relative_volume(150, (100, 100, 100), minimum_sessions=3).value == Decimal(
        "1.5"
    )
    assert (
        relative_volume(150, (100, 100), minimum_sessions=3).status
        is IndicatorStatus.NOT_READY
    )
    strength = relative_strength(
        stock_open=Decimal("100"),
        stock_current=Decimal("110"),
        benchmark_open=Decimal("100"),
        benchmark_current=Decimal("105"),
    )
    assert strength.value == Decimal("5.00")

    candles = [
        _candle(
            index,
            start_price=Decimal("100"),
            step=Decimal("0.1"),
        )
        for index in range(15)
    ]
    result = opening_range(candles, minutes=15)
    assert result.value is not None
    assert result.value.high == Decimal("101.45")
    assert result.value.low == Decimal("99.95")
    assert opening_range(candles[:14], minutes=15).status is IndicatorStatus.NOT_READY


def test_market_regime_fails_closed_and_classifies_trend() -> None:
    benchmark = [
        _candle(
            index,
            interval=CandleInterval.FIVE_MINUTES,
            start_price=Decimal("100"),
            step=Decimal("0.1"),
            token="999",
            symbol="NIFTY",
        )
        for index in range(20)
    ]

    assert (
        classify_market_regime(
            benchmark,
            _health("999", status=MarketDataHealthStatus.STALE),
        )
        is MarketRegime.UNKNOWN
    )
    assert classify_market_regime(benchmark, _health("999")) is MarketRegime.TREND_UP
    assert (
        classify_market_regime(benchmark[:19], _health("999")) is MarketRegime.UNKNOWN
    )

    declining = [
        _candle(
            index,
            interval=CandleInterval.FIVE_MINUTES,
            start_price=Decimal("102"),
            step=Decimal("-0.1"),
            token="999",
            symbol="NIFTY",
        )
        for index in range(20)
    ]
    flat = [
        _candle(
            index,
            interval=CandleInterval.FIVE_MINUTES,
            start_price=Decimal("100"),
            step=Decimal("0"),
            token="999",
            symbol="NIFTY",
        )
        for index in range(20)
    ]
    assert classify_market_regime(declining, _health("999")) is MarketRegime.TREND_DOWN
    assert classify_market_regime(flat, _health("999")) is MarketRegime.RANGE


class FixedBaseline:
    def load_rvol_baselines(self, **_kwargs):
        return (100,) * 10


def _scanner_fixture():
    first = BrokerInstrument(Exchange.NSE_CM, "1", "ABC-EQ", Decimal("0.05"))
    second = BrokerInstrument(Exchange.NSE_CM, "2", "XYZ-EQ", Decimal("0.05"))
    benchmark = BrokerInstrument(
        Exchange.NSE_CM,
        "999",
        "NIFTY",
        Decimal("0.05"),
        instrument_type="AMXIDX",
    )
    stock_one = [
        _candle(
            index,
            start_price=Decimal("99.80"),
            step=Decimal("0.01"),
        )
        for index in range(20)
    ]
    stock_five = [
        _candle(
            index,
            interval=CandleInterval.FIVE_MINUTES,
            start_price=Decimal("99.5"),
            step=Decimal("0.03"),
        )
        for index in range(20)
    ]
    second_candles = [
        Candle(
            exchange=candle.exchange,
            symbol_token="2",
            trading_symbol="XYZ-EQ",
            interval=candle.interval,
            start_at=candle.start_at,
            end_at=candle.end_at,
            open_price=candle.open_price,
            high_price=candle.high_price,
            low_price=candle.low_price,
            close_price=candle.close_price,
            volume=candle.volume,
            tick_count=candle.tick_count,
            completed=True,
            trusted=True,
        )
        for candle in stock_one + stock_five
    ]
    benchmark_candles = [
        _candle(
            index,
            start_price=Decimal("100"),
            step=Decimal("0.005"),
            token="999",
            symbol="NIFTY",
        )
        for index in range(20)
    ] + [
        _candle(
            index,
            interval=CandleInterval.FIVE_MINUTES,
            start_price=Decimal("100"),
            step=Decimal("0.1"),
            token="999",
            symbol="NIFTY",
        )
        for index in range(20)
    ]
    candles = {
        (Exchange.NSE_CM, "1"): stock_one + stock_five,
        (Exchange.NSE_CM, "2"): second_candles,
        (Exchange.NSE_CM, "999"): benchmark_candles,
    }
    health = {
        (Exchange.NSE_CM, "1"): _health("1"),
        (Exchange.NSE_CM, "2"): _health("2"),
        (Exchange.NSE_CM, "999"): _health("999"),
    }
    return first, second, benchmark, candles, health


def test_scanner_exact_score_and_deterministic_tie_breaking() -> None:
    first, second, benchmark, candles, health = _scanner_fixture()
    scanner = MarketScanner(
        weights=ScannerWeights(),
        relative_volume_minimum_sessions=10,
        relative_volume_lookback_sessions=15,
    )

    result = scanner.scan(
        instruments=(second, first),
        candles_by_instrument=candles,
        health_by_instrument=health,
        benchmark=benchmark,
        baseline_store=FixedBaseline(),
        now=BASE + timedelta(minutes=20),
    )

    assert result.readiness is ScannerReadiness.READY
    assert [item.symbol for item in result.long_candidates] == ["ABC-EQ", "XYZ-EQ"]
    assert result.long_candidates[0].total_score == Decimal("10")
    assert result.long_candidates[0].component_scores == {
        "vwap": Decimal("2"),
        "ema": Decimal("2"),
        "relative_volume": Decimal("2"),
        "relative_strength": Decimal("2"),
        "opening_range_proximity": Decimal("1"),
        "market_regime": Decimal("1"),
    }
    assert (
        result.to_dict()
        == scanner.scan(
            instruments=(second, first),
            candles_by_instrument=candles,
            health_by_instrument=health,
            benchmark=benchmark,
            baseline_store=FixedBaseline(),
            now=BASE + timedelta(minutes=20),
        ).to_dict()
    )


def test_scanner_suppresses_stale_data_and_unknown_regime() -> None:
    first, _, benchmark, candles, health = _scanner_fixture()
    scanner = MarketScanner()
    health[(Exchange.NSE_CM, "1")] = _health("1", status=MarketDataHealthStatus.STALE)
    stale = scanner.scan(
        instruments=(first,),
        candles_by_instrument=candles,
        health_by_instrument=health,
        benchmark=benchmark,
        baseline_store=FixedBaseline(),
        now=BASE + timedelta(minutes=20),
    )
    assert stale.readiness is ScannerReadiness.DEGRADED
    assert not stale.long_candidates
    assert stale.unhealthy_count == 1

    health[(Exchange.NSE_CM, "1")] = _health("1")
    health[(Exchange.NSE_CM, "999")] = _health(
        "999", status=MarketDataHealthStatus.STALE
    )
    unknown = scanner.scan(
        instruments=(first,),
        candles_by_instrument=candles,
        health_by_instrument=health,
        benchmark=benchmark,
        baseline_store=FixedBaseline(),
        now=BASE + timedelta(minutes=20),
    )
    assert unknown.readiness is ScannerReadiness.NOT_READY
    assert unknown.market_regime is MarketRegime.UNKNOWN
    assert not unknown.long_candidates
