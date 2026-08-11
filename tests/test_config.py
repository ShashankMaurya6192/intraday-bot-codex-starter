from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_bot.config import Settings
from trading_bot.domain import Exchange, SubscriptionMode, TradingMode


def test_default_is_paper() -> None:
    settings = Settings(_env_file=None)

    assert settings.trading_mode is TradingMode.PAPER
    assert settings.live_trading_enabled is False
    assert settings.risk_per_trade_pct == Decimal("0.003")
    assert settings.websocket_stable_connection_seconds == 30
    assert settings.tick_ingestion_queue_capacity == 10_000
    assert settings.tick_ingestion_batch_size == 250
    assert settings.instrument_master_max_age_days == 7
    assert settings.intraday_universe_max_age_seconds == 28_800
    assert settings.relative_volume_lookback_sessions == 15
    assert settings.relative_volume_minimum_sessions == 10
    assert settings.historical_bootstrap_lookback_days == 30
    assert settings.historical_bootstrap_minimum_sessions == 15
    assert settings.scan_replay_hard_tick_limit == 2_000_000
    assert settings.scanner_projection_stale_after_seconds == 5
    assert settings.scanner_projection_catch_up_tick_limit == 10_000
    assert not hasattr(settings, "market_data_allowed_sequence_gap")
    assert settings.configured_scanner_symbols() == ()


def test_live_requires_both_feature_gate_and_startup_confirmation() -> None:
    disabled = Settings(
        _env_file=None,
        trading_mode="LIVE",
        live_trading_enabled=False,
    )
    with pytest.raises(RuntimeError, match="LIVE_TRADING_ENABLED"):
        disabled.assert_live_allowed()

    gated = Settings(
        _env_file=None,
        trading_mode="LIVE",
        live_trading_enabled=True,
    )
    with pytest.raises(RuntimeError, match="explicit startup confirmation"):
        gated.assert_live_allowed()

    gated.assert_live_allowed(explicit_startup_confirmation=True)


def test_market_subscriptions_are_parsed_and_deduplicated() -> None:
    settings = Settings(
        _env_file=None,
        angel_market_subscriptions="NSE_CM:3045:SBIN-EQ, NSE_CM:26000:NIFTY",
        angel_subscription_mode="QUOTE",
    )

    subscriptions = settings.market_subscriptions()

    assert len(subscriptions) == 2
    assert subscriptions[0].exchange is Exchange.NSE_CM
    assert subscriptions[0].symbol_token == "3045"
    assert subscriptions[0].trading_symbol == "SBIN-EQ"
    assert settings.angel_subscription_mode is SubscriptionMode.QUOTE

    duplicate = Settings(
        _env_file=None,
        angel_market_subscriptions="NSE_CM:3045,NSE_CM:3045:SBIN-EQ",
    )
    with pytest.raises(ValueError, match="duplicate"):
        duplicate.market_subscriptions()


def test_invalid_risk_and_non_sqlite_database_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            risk_per_trade_pct="0.02",
            max_daily_loss_pct="0.01",
        )

    with pytest.raises(ValidationError, match="sqlite"):
        Settings(_env_file=None, database_url="postgresql://localhost/trading")


def test_secrets_are_redacted() -> None:
    settings = Settings(
        _env_file=None,
        angel_api_key="api-secret",
        angel_client_code="client-secret",
        angel_password="pin-secret",
        angel_totp_secret="totp-secret",
    )

    rendered = repr(settings)
    assert "api-secret" not in rendered
    assert "pin-secret" not in rendered
    assert settings.angel_credentials_configured is True


def test_ingestion_batch_cannot_exceed_queue_capacity() -> None:
    with pytest.raises(ValidationError, match="batch size"):
        Settings(
            _env_file=None,
            tick_ingestion_queue_capacity=10,
            tick_ingestion_batch_size=11,
        )


def test_scanner_symbol_override_and_rvol_warmup_are_validated() -> None:
    settings = Settings(_env_file=None, scanner_symbols="SBIN, RELIANCE")
    assert settings.configured_scanner_symbols() == ("SBIN", "RELIANCE")

    with pytest.raises(ValidationError, match="minimum sessions"):
        Settings(
            _env_file=None,
            relative_volume_lookback_sessions=5,
            relative_volume_minimum_sessions=6,
        )

    duplicate = Settings(_env_file=None, scanner_symbols="SBIN,SBIN")
    with pytest.raises(ValueError, match="duplicates"):
        duplicate.configured_scanner_symbols()


def test_projection_freshness_and_catch_up_bounds_are_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, scanner_projection_stale_after_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, scanner_projection_catch_up_tick_limit=0)
