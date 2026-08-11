from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_bot.config import Settings
from trading_bot.domain import IST, Exchange
from trading_bot.marketdata import (
    Candle,
    CandleInterval,
    MarketDataHealthStatus,
    MarketRegime,
)
from trading_bot.scanner import (
    CandidateBias,
    ScannerCandidate,
    ScannerDiagnostics,
    ScannerReadiness,
    ScannerResult,
    qualify_candidate,
)

SESSION = date(2026, 8, 10)
EVALUATION = datetime.combine(SESSION, time(9, 35), tzinfo=IST)


def _candidate(bias: CandidateBias = CandidateBias.LONG) -> ScannerCandidate:
    if bias is CandidateBias.LONG:
        ltp = Decimal("110")
        vwap = Decimal("109")
        ema9 = Decimal("108")
        ema20 = Decimal("107")
        strength = Decimal("0.5")
        regime = MarketRegime.TREND_UP
    else:
        ltp = Decimal("90")
        vwap = Decimal("91")
        ema9 = Decimal("90")
        ema20 = Decimal("92")
        strength = Decimal("-0.5")
        regime = MarketRegime.TREND_DOWN
    return ScannerCandidate(
        bias=bias,
        symbol="ABC-EQ",
        symbol_token="1",
        ltp=ltp,
        data_age_seconds=0.1,
        health=MarketDataHealthStatus.READY,
        vwap=vwap,
        vwap_distance_pct=Decimal("0.9"),
        vwap_distance_atr=Decimal("1"),
        ema9=ema9,
        ema20=ema20,
        atr14=Decimal("1"),
        relative_volume=Decimal("1.5"),
        relative_strength=strength,
        opening_range_high=Decimal("109.5"),
        opening_range_low=Decimal("90.5"),
        distance_to_breakout_pct=Decimal("-0.45"),
        distance_to_breakdown_pct=Decimal("17.72"),
        sector=None,
        market_regime=regime,
        component_scores={"market_regime": Decimal("1")},
        total_score=Decimal("10"),
    )


def _confirmation(
    close: Decimal,
    *,
    start_at: datetime | None = None,
    completed: bool = True,
    trusted: bool = True,
) -> Candle:
    start = start_at or datetime.combine(SESSION, time(9, 30), tzinfo=IST)
    return Candle(
        exchange=Exchange.NSE_CM,
        symbol_token="1",
        trading_symbol="ABC-EQ",
        interval=CandleInterval.FIVE_MINUTES,
        start_at=start,
        end_at=start + timedelta(minutes=5),
        open_price=Decimal("100"),
        high_price=max(Decimal("101"), close),
        low_price=min(Decimal("99"), close),
        close_price=close,
        volume=100,
        tick_count=5,
        completed=completed,
        trusted=trusted,
    )


def _qualify(
    candidate: ScannerCandidate,
    candles: tuple[Candle, ...] | None = None,
    *,
    evaluation_at: datetime = EVALUATION,
    max_extension: Decimal = Decimal("1.5"),
):
    if candles is None:
        close = (
            candidate.opening_range_high + Decimal("0.1")
            if candidate.bias is CandidateBias.LONG
            else candidate.opening_range_low - Decimal("0.1")
        )
        candles = (_confirmation(close),)
    return qualify_candidate(
        candidate,
        current_session_five_minute=candles,
        session_date=SESSION,
        evaluation_at=evaluation_at,
        opening_range_minutes=15,
        relative_volume_threshold=Decimal("1.5"),
        max_vwap_distance_atr=max_extension,
    )


def test_high_long_score_in_downtrend_is_rankable_but_not_qualified() -> None:
    candidate = replace(
        _candidate(),
        market_regime=MarketRegime.TREND_DOWN,
        total_score=Decimal("9"),
    )

    result = _qualify(candidate)

    assert candidate.total_score == Decimal("9")
    assert not result.qualified
    assert result.reasons == ("market_regime_not_trend_up",)


def test_rvol_below_threshold_fails_and_equality_passes() -> None:
    below = _qualify(replace(_candidate(), relative_volume=Decimal("1.49")))
    equal = _qualify(replace(_candidate(), relative_volume=Decimal("1.5")))

    assert below.reasons == ("rvol_below_threshold",)
    assert equal.qualified


def test_tick_price_above_opening_high_without_completed_close_fails() -> None:
    candidate = replace(_candidate(), ltp=Decimal("111"))

    result = _qualify(candidate, ())

    assert result.reasons == ("opening_breakout_not_confirmed",)


def test_completed_trusted_close_strictly_above_opening_high_confirms() -> None:
    candidate = _candidate()

    result = _qualify(candidate, (_confirmation(Decimal("109.51")),))

    assert result.qualified


def test_close_equal_to_boundary_or_at_opening_range_end_does_not_confirm() -> None:
    candidate = _candidate()
    equal_close = _confirmation(candidate.opening_range_high)
    range_end = _confirmation(
        Decimal("110"),
        start_at=datetime.combine(SESSION, time(9, 25), tzinfo=IST),
    )

    assert _qualify(candidate, (equal_close,)).reasons == (
        "opening_breakout_not_confirmed",
    )
    assert _qualify(candidate, (range_end,)).reasons == (
        "opening_breakout_not_confirmed",
    )


def test_prior_session_confirmation_cannot_qualify_current_session() -> None:
    prior = _confirmation(
        Decimal("110"),
        start_at=datetime.combine(
            SESSION - timedelta(days=1), time(9, 30), tzinfo=IST
        ),
    )

    result = _qualify(_candidate(), (prior,))

    assert result.reasons == ("opening_breakout_not_confirmed",)


@pytest.mark.parametrize(
    ("completed", "trusted"),
    ((False, True), (True, False)),
)
def test_partial_or_untrusted_five_minute_candle_cannot_qualify(
    completed: bool, trusted: bool
) -> None:
    candle = _confirmation(
        Decimal("110"), completed=completed, trusted=trusted
    )

    result = _qualify(_candidate(), (candle,))

    assert result.reasons == ("opening_breakout_not_confirmed",)


def test_future_confirmation_is_hidden_by_as_of_cutoff() -> None:
    candle = _confirmation(Decimal("110"))

    before = _qualify(
        _candidate(),
        (candle,),
        evaluation_at=datetime.combine(SESSION, time(9, 34, 59), tzinfo=IST),
    )
    at_close = _qualify(_candidate(), (candle,), evaluation_at=EVALUATION)

    assert before.reasons == ("opening_breakout_not_confirmed",)
    assert at_close.qualified


def test_long_nonpositive_and_short_nonnegative_relative_strength_fail() -> None:
    long_result = _qualify(replace(_candidate(), relative_strength=Decimal("0")))
    short_result = _qualify(
        replace(
            _candidate(CandidateBias.SHORT),
            relative_strength=Decimal("0"),
        )
    )

    assert long_result.reasons == ("relative_strength_not_positive",)
    assert short_result.reasons == ("relative_strength_not_negative",)


def test_non_ready_market_data_cannot_qualify() -> None:
    candidate = replace(
        _candidate(), health=MarketDataHealthStatus.DEGRADED
    )

    result = _qualify(candidate)

    assert result.reasons == ("market_data_not_ready",)


@pytest.mark.parametrize("bias", (CandidateBias.LONG, CandidateBias.SHORT))
def test_ema_equality_fails_for_both_sides(bias: CandidateBias) -> None:
    candidate = replace(_candidate(bias), ema9=Decimal("100"), ema20=Decimal("100"))

    result = _qualify(candidate)

    expected = (
        "ema9_not_above_ema20"
        if bias is CandidateBias.LONG
        else "ema9_not_below_ema20"
    )
    assert result.reasons == (expected,)


@pytest.mark.parametrize("bias", (CandidateBias.LONG, CandidateBias.SHORT))
def test_vwap_equality_fails_for_both_sides(bias: CandidateBias) -> None:
    candidate = _candidate(bias)
    candidate = replace(candidate, ltp=candidate.vwap)

    result = _qualify(candidate)

    expected = (
        "price_not_above_vwap"
        if bias is CandidateBias.LONG
        else "price_not_below_vwap"
    )
    assert result.reasons == (expected,)


def test_extension_above_max_fails_and_equality_passes() -> None:
    exceeded = _qualify(
        replace(_candidate(), vwap_distance_atr=Decimal("-1.5001"))
    )
    equal = _qualify(replace(_candidate(), vwap_distance_atr=Decimal("-1.5")))

    assert exceeded.reasons == ("vwap_atr_extension_exceeded",)
    assert equal.qualified


@pytest.mark.parametrize("regime", (MarketRegime.RANGE, MarketRegime.UNKNOWN))
@pytest.mark.parametrize("bias", (CandidateBias.LONG, CandidateBias.SHORT))
def test_range_and_unknown_qualify_neither_side(
    regime: MarketRegime, bias: CandidateBias
) -> None:
    result = _qualify(replace(_candidate(bias), market_regime=regime))

    expected = (
        "market_regime_not_trend_up"
        if bias is CandidateBias.LONG
        else "market_regime_not_trend_down"
    )
    assert result.reasons == (expected,)


def test_all_gates_long_and_short_qualify() -> None:
    assert _qualify(_candidate(CandidateBias.LONG)).qualified
    assert _qualify(_candidate(CandidateBias.SHORT)).qualified


def test_qualification_reason_order_is_deterministic() -> None:
    candidate = replace(
        _candidate(),
        market_regime=MarketRegime.RANGE,
        ltp=Decimal("109"),
        vwap=Decimal("109"),
        ema9=Decimal("107"),
        ema20=Decimal("107"),
        relative_volume=Decimal("1.49"),
        relative_strength=Decimal("0"),
        vwap_distance_atr=Decimal("2"),
    )

    first = _qualify(candidate, ())
    second = _qualify(candidate, ())

    assert first == second
    assert first.reasons == (
        "market_regime_not_trend_up",
        "price_not_above_vwap",
        "ema9_not_above_ema20",
        "rvol_below_threshold",
        "relative_strength_not_positive",
        "opening_breakout_not_confirmed",
        "vwap_atr_extension_exceeded",
    )


def test_real_live_regressions_do_not_qualify() -> None:
    sbin = replace(
        _candidate(),
        symbol="SBIN-EQ",
        ltp=Decimal("1069.6"),
        vwap=Decimal("1065.55"),
        ema9=Decimal("1066.6497"),
        ema20=Decimal("1062.1592"),
        relative_volume=Decimal("1.0905"),
        relative_strength=Decimal("0.3875"),
        opening_range_high=Decimal("1071"),
        opening_range_low=Decimal("1060.4"),
        market_regime=MarketRegime.TREND_DOWN,
        total_score=Decimal("8"),
    )
    hdfc = replace(
        _candidate(CandidateBias.SHORT),
        symbol="HDFCBANK-EQ",
        ltp=Decimal("727.85"),
        vwap=Decimal("727.97"),
        ema9=Decimal("727.9111"),
        ema20=Decimal("729.8224"),
        relative_volume=Decimal("0.8991"),
        relative_strength=Decimal("0.1554"),
        opening_range_low=Decimal("727.3"),
        total_score=Decimal("7"),
    )

    sbin_result = _qualify(sbin, (_confirmation(Decimal("1072")),))
    hdfc_result = _qualify(hdfc, (_confirmation(Decimal("727")),))

    assert sbin_result.reasons == (
        "market_regime_not_trend_up",
        "rvol_below_threshold",
    )
    assert hdfc_result.reasons == (
        "rvol_below_threshold",
        "relative_strength_not_negative",
    )


def test_ranked_candidate_remains_available_when_unqualified() -> None:
    candidate = replace(
        _candidate(),
        qualified=False,
        qualification_reasons=("market_regime_not_trend_up",),
    )
    result = ScannerResult(
        readiness=ScannerReadiness.READY,
        market_regime=MarketRegime.TREND_DOWN,
        long_candidates=(candidate,),
        short_candidates=(),
        evaluated_count=1,
        rejected_count=0,
        unhealthy_count=0,
        requested_count=1,
        eligible_count=1,
        expected_ineligible_count=0,
        mapping_failure_count=0,
        indicator_not_ready_count=0,
        benchmark_failure_count=0,
        raw_ticks_replayed=100,
        replay_elapsed_seconds=0.1,
        diagnostics=ScannerDiagnostics(),
    )
    payload = result.to_dict()

    assert len(result.long_candidates) == 1
    assert result.qualified_long_signals == ()
    assert payload["long_candidates"][0]["qualified"] is False
    assert payload["qualified_long_count"] == 0


def test_max_vwap_distance_configuration_is_positive_and_defaults_to_1_5() -> None:
    assert Settings(_env_file=None).max_vwap_distance_atr == Decimal("1.5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_vwap_distance_atr=Decimal("0"))
