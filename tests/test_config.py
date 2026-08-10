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
