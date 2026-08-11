from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

import trading_bot.__main__ as cli
from trading_bot.config import Settings
from trading_bot.domain import IST, Exchange, MarketTick, SubscriptionMode
from trading_bot.marketdata import (
    Candle,
    CandleInterval,
    MarketReplayEngine,
    ReplaySessionNotCompleteError,
)
from trading_bot.storage import SQLiteMarketRecorder

SESSION = date(2026, 8, 12)
OPEN = datetime(2026, 8, 12, 9, 15, tzinfo=IST)


def _tick(minute: int, *, sequence: int, price: str = "100") -> MarketTick:
    exchange_at = OPEN + timedelta(minutes=minute, seconds=10)
    return MarketTick(
        exchange=Exchange.NSE_CM,
        symbol_token="1",
        trading_symbol="ABC-EQ",
        subscription_mode=SubscriptionMode.QUOTE,
        sequence_number=sequence,
        exchange_timestamp=exchange_at,
        received_at=exchange_at + timedelta(milliseconds=10),
        last_traded_price=Decimal(price),
        volume_trade_for_the_day=1_000 + minute * 100,
    )


def _canonical_candle() -> Candle:
    return Candle(
        exchange=Exchange.NSE_CM,
        symbol_token="1",
        trading_symbol="ABC-EQ",
        interval=CandleInterval.ONE_MINUTE,
        start_at=OPEN,
        end_at=OPEN + timedelta(minutes=1),
        open_price=Decimal("999"),
        high_price=Decimal("999"),
        low_price=Decimal("999"),
        close_price=Decimal("999"),
        volume=9_999,
        tick_count=99,
        completed=True,
        trusted=True,
    )


def _engine() -> MarketReplayEngine:
    return MarketReplayEngine(stale_after_seconds=300)


def test_current_date_before_close_cannot_persist_or_overwrite_canonical_state() -> (
    None
):
    canonical = _canonical_candle()
    before_close = OPEN.replace(hour=15, minute=29, second=59)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_tick(_tick(0, sequence=1))
        recorder.record_candles((canonical,))

        with pytest.raises(
            ReplaySessionNotCompleteError,
            match="current_session_not_complete",
        ):
            _engine().replay_date(
                recorder,
                SESSION,
                persist=True,
                current_time=before_close,
            )

        stored = recorder.load_candles()
        baselines = recorder.load_rvol_baselines(
            exchange=Exchange.NSE_CM,
            symbol_token="1",
            before_date=SESSION + timedelta(days=1),
            minute_offset=0,
            lookback_sessions=10,
        )

    assert stored == [canonical]
    assert baselines == ()


def test_current_date_at_session_close_can_persist_full_replay() -> None:
    at_close = OPEN.replace(hour=15, minute=30, second=0)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_ticks((_tick(0, sequence=1), _tick(1, sequence=2)))

        result = _engine().replay_date(
            recorder,
            SESSION,
            persist=True,
            current_time=at_close,
        )
        stored = recorder.load_candles()

    assert result.candles
    assert stored == list(result.candles)
    assert all(candle.end_at <= at_close for candle in stored)


def test_prior_completed_session_persistence_remains_backward_compatible() -> None:
    current_time = OPEN + timedelta(days=1)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_ticks((_tick(0, sequence=1), _tick(1, sequence=2)))

        result = _engine().replay_date(
            recorder,
            SESSION,
            persist=True,
            current_time=current_time,
        )

        assert recorder.load_candles() == list(result.candles)


def test_explicit_as_of_replay_is_ephemeral_even_before_close() -> None:
    before_close = OPEN.replace(hour=10)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_ticks((_tick(0, sequence=1), _tick(1, sequence=2)))

        result = _engine().replay_date(
            recorder,
            SESSION,
            evaluation_at=before_close,
            persist=True,
            current_time=before_close,
        )

        assert result.candles
        assert recorder.load_candles() == []
        assert recorder.load_rvol_baselines(
            exchange=Exchange.NSE_CM,
            symbol_token="1",
            before_date=SESSION + timedelta(days=1),
            minute_offset=0,
            lookback_sessions=10,
        ) == ()


def test_incomplete_after_close_replay_preserves_existing_rvol_baseline() -> None:
    after_close = OPEN.replace(hour=15, minute=31)
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        recorder.record_ticks((_tick(0, sequence=1), _tick(1, sequence=2)))
        recorder.record_volume_baseline_session(
            exchange=Exchange.NSE_CM,
            symbol_token="1",
            session_date=SESSION,
            cumulative_by_minute={0: 9_999},
            complete=True,
            trusted=True,
        )

        _engine().replay_date(
            recorder,
            SESSION,
            persist=True,
            current_time=after_close,
        )
        preserved = recorder.load_rvol_baselines(
            exchange=Exchange.NSE_CM,
            symbol_token="1",
            before_date=SESSION + timedelta(days=1),
            minute_offset=0,
            lookback_sessions=10,
        )

    assert preserved == (9_999,)


def test_future_session_persistence_is_rejected() -> None:
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        with pytest.raises(
            ReplaySessionNotCompleteError,
            match="future_session_not_complete",
        ):
            _engine().replay_date(
                recorder,
                SESSION + timedelta(days=1),
                persist=True,
                current_time=OPEN,
            )


def test_replay_cli_reports_current_session_rejection(
    monkeypatch, tmp_path, capsys
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'replay-safety.db'}",
    )
    monkeypatch.setattr(cli, "_ist_now", lambda: OPEN.replace(hour=10))

    exit_code = cli._replay(settings, SESSION, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["status"] == "REJECTED"
    assert payload["reason"] == "current_session_not_complete"
    assert payload["persisted"] is False
    assert payload["order_placement_available"] is False
