from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from trading_bot.broker import BrokerInstrument
from trading_bot.domain import IST, Exchange
from trading_bot.marketdata import (
    Candle,
    CandleInterval,
    MarketDataHealthStatus,
    MarketRegime,
)
from trading_bot.risk import TradeSide
from trading_bot.scanner import CandidateBias, ScannerCandidate
from trading_bot.strategy import (
    PaperIntentRejection,
    RelativeStrengthOpeningBreakoutV1IntentPolicy,
)


class _AllowedUniverse:
    def __init__(self, allowed: bool = True, broken: bool = False) -> None:
        self.allowed = allowed
        self.broken = broken

    def is_intraday_allowed(self, _instrument: BrokerInstrument) -> bool:
        if self.broken:
            raise RuntimeError("cache unavailable")
        return self.allowed


def _candidate(
    bias: CandidateBias = CandidateBias.LONG,
) -> ScannerCandidate:
    is_long = bias is CandidateBias.LONG
    return ScannerCandidate(
        bias=bias,
        symbol="SBIN-EQ",
        symbol_token="3045",
        ltp=Decimal("101") if is_long else Decimal("99"),
        data_age_seconds=1.0,
        health=MarketDataHealthStatus.READY,
        vwap=Decimal("100") if is_long else Decimal("100.5"),
        vwap_distance_pct=Decimal("1"),
        vwap_distance_atr=Decimal("0.5"),
        ema9=Decimal("100.5") if is_long else Decimal("99.5"),
        ema20=Decimal("100") if is_long else Decimal("100"),
        atr14=Decimal("2"),
        relative_volume=Decimal("1.8"),
        relative_strength=Decimal("0.5") if is_long else Decimal("-0.5"),
        opening_range_high=Decimal("100"),
        opening_range_low=Decimal("100"),
        distance_to_breakout_pct=Decimal("-1"),
        distance_to_breakdown_pct=Decimal("1"),
        sector=None,
        market_regime=MarketRegime.TREND_UP if is_long else MarketRegime.TREND_DOWN,
        qualified=True,
    )


def _instrument() -> BrokerInstrument:
    return BrokerInstrument(
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        tick_size=Decimal("0.05"),
        lot_size=1,
        instrument_type="EQ",
    )


def _confirmation(bias: CandidateBias = CandidateBias.LONG) -> Candle:
    is_long = bias is CandidateBias.LONG
    return Candle(
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        interval=CandleInterval.FIVE_MINUTES,
        start_at=datetime(2026, 10, 21, 9, 30, tzinfo=IST),
        end_at=datetime(2026, 10, 21, 9, 35, tzinfo=IST),
        open_price=Decimal("99.5") if is_long else Decimal("100.5"),
        high_price=Decimal("101.5"),
        low_price=Decimal("98.5"),
        close_price=Decimal("101") if is_long else Decimal("99"),
        volume=10_000,
        tick_count=20,
        completed=True,
        trusted=True,
    )


def test_qualified_long_builds_idempotent_paper_intent_with_initial_stop() -> None:
    decision = RelativeStrengthOpeningBreakoutV1IntentPolicy().evaluate(
        _candidate(),
        instrument=_instrument(),
        intraday_universe=_AllowedUniverse(),
        current_session_five_minute=(_confirmation(),),
        evaluated_at=datetime(2026, 10, 21, 9, 36, tzinfo=IST),
        max_liquidity_quantity=1_000,
    )

    assert decision.accepted
    assert decision.intent is not None
    assert decision.intent.side is TradeSide.LONG
    assert decision.intent.protective_stop == Decimal("99.80")
    assert decision.intent.client_tag == "rsobv1:20261021:NSE_CM:3045:L:0935"
    assert decision.to_dict()["broker_order_created"] is False


def test_short_stop_is_above_entry_and_uses_same_structural_policy() -> None:
    decision = RelativeStrengthOpeningBreakoutV1IntentPolicy().evaluate(
        _candidate(CandidateBias.SHORT),
        instrument=_instrument(),
        intraday_universe=_AllowedUniverse(),
        current_session_five_minute=(_confirmation(CandidateBias.SHORT),),
        evaluated_at=datetime(2026, 10, 21, 9, 36, tzinfo=IST),
        max_liquidity_quantity=1_000,
    )

    assert decision.intent is not None
    assert decision.intent.side is TradeSide.SHORT
    assert decision.intent.protective_stop == Decimal("100.20")


def test_policy_rechecks_stale_breakout_eligibility_and_liquidity() -> None:
    candidate = replace(
        _candidate(),
        ltp=Decimal("99.90"),
        data_age_seconds=6.0,
    )
    decision = RelativeStrengthOpeningBreakoutV1IntentPolicy().evaluate(
        candidate,
        instrument=_instrument(),
        intraday_universe=_AllowedUniverse(allowed=False),
        current_session_five_minute=(_confirmation(),),
        evaluated_at=datetime(2026, 10, 21, 9, 36, tzinfo=IST),
        max_liquidity_quantity=None,
    )

    assert not decision.accepted
    assert decision.intent is None
    assert decision.rejection_reasons == (
        PaperIntentRejection.MARKET_DATA_STALE,
        PaperIntentRejection.INSTRUMENT_NOT_INTRADAY_ALLOWED,
        PaperIntentRejection.LIQUIDITY_UNVERIFIED,
        PaperIntentRejection.BREAKOUT_NO_LONGER_ACTIVE,
    )


def test_policy_fails_closed_when_intraday_cache_raises() -> None:
    decision = RelativeStrengthOpeningBreakoutV1IntentPolicy().evaluate(
        _candidate(),
        instrument=_instrument(),
        intraday_universe=_AllowedUniverse(broken=True),
        current_session_five_minute=(_confirmation(),),
        evaluated_at=datetime(2026, 10, 21, 9, 36, tzinfo=IST),
        max_liquidity_quantity=100,
    )

    assert decision.rejection_reasons == (
        PaperIntentRejection.INTRADAY_ELIGIBILITY_UNAVAILABLE,
    )
