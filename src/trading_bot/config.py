"""Environment-backed application configuration with fail-closed safety gates."""

from __future__ import annotations

from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain import Exchange, MarketSubscription, SubscriptionMode, TradingMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    trading_mode: TradingMode = TradingMode.PAPER
    live_trading_enabled: bool = False

    angel_api_key: SecretStr = SecretStr("")
    angel_client_code: SecretStr = SecretStr("")
    angel_password: SecretStr = SecretStr("")
    angel_totp_secret: SecretStr = SecretStr("")
    angel_market_subscriptions: str = ""
    angel_subscription_mode: SubscriptionMode = SubscriptionMode.QUOTE
    angel_correlation_id: str = "intraday01"

    database_url: str = "sqlite:///trading_bot.db"
    database_busy_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)
    market_timezone: str = "Asia/Kolkata"
    market_data_stale_after_seconds: float = Field(default=5.0, gt=0, le=300)

    websocket_max_reconnect_attempts: int = Field(default=5, ge=0, le=20)
    websocket_reconnect_initial_delay_seconds: float = Field(default=1.0, ge=0, le=60)
    websocket_reconnect_multiplier: float = Field(default=2.0, ge=1, le=10)
    websocket_reconnect_max_delay_seconds: float = Field(default=30.0, ge=0, le=300)

    risk_per_trade_pct: Decimal = Field(default=Decimal("0.003"), gt=0, le=1)
    max_daily_loss_pct: Decimal = Field(default=Decimal("0.009"), gt=0, le=1)
    max_open_positions: int = Field(default=2, ge=1)
    max_trades_per_day: int = Field(default=4, ge=1)
    max_consecutive_losses: int = Field(default=3, ge=1)
    max_capital_utilization_pct: Decimal = Field(default=Decimal("1.0"), gt=0, le=1)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("sqlite:///"):
            raise ValueError("only sqlite:/// database URLs are supported in Phase 1")
        if not value.removeprefix("sqlite:///"):
            raise ValueError("database_url must include a SQLite path")
        return value

    @field_validator("market_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        if value != "Asia/Kolkata":
            raise ValueError("market_timezone must be Asia/Kolkata")
        return value

    @field_validator("angel_correlation_id")
    @classmethod
    def validate_correlation_id(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 10 or not value.isalnum():
            raise ValueError(
                "angel_correlation_id must be 1-10 alphanumeric characters"
            )
        return value

    @model_validator(mode="after")
    def validate_risk_relationships(self) -> Settings:
        if self.risk_per_trade_pct > self.max_daily_loss_pct:
            raise ValueError("risk_per_trade_pct cannot exceed max_daily_loss_pct")
        if (
            self.websocket_reconnect_initial_delay_seconds
            > self.websocket_reconnect_max_delay_seconds
        ):
            raise ValueError(
                "websocket reconnect initial delay cannot exceed maximum delay"
            )
        return self

    @property
    def angel_credentials_configured(self) -> bool:
        return all(
            secret.get_secret_value().strip()
            for secret in (
                self.angel_api_key,
                self.angel_client_code,
                self.angel_password,
                self.angel_totp_secret,
            )
        )

    def market_subscriptions(self) -> tuple[MarketSubscription, ...]:
        """Parse EXCHANGE:TOKEN[:SYMBOL] entries from the environment."""
        if not self.angel_market_subscriptions.strip():
            return ()

        subscriptions: list[MarketSubscription] = []
        seen: set[tuple[Exchange, str]] = set()
        for raw_entry in self.angel_market_subscriptions.split(","):
            entry = raw_entry.strip()
            parts = [part.strip() for part in entry.split(":", maxsplit=2)]
            if len(parts) not in (2, 3):
                raise ValueError(
                    "ANGEL_MARKET_SUBSCRIPTIONS entries must use "
                    "EXCHANGE:TOKEN[:SYMBOL]"
                )
            try:
                exchange = Exchange(parts[0].upper())
            except ValueError as exc:
                raise ValueError(
                    f"unsupported exchange in market subscription: {parts[0]}"
                ) from exc

            token = parts[1]
            if not token.isdigit():
                raise ValueError("Angel One symbol tokens must contain only digits")
            key = (exchange, token)
            if key in seen:
                raise ValueError(
                    f"duplicate market subscription: {exchange.value}:{token}"
                )
            seen.add(key)
            subscriptions.append(
                MarketSubscription(
                    exchange=exchange,
                    symbol_token=token,
                    trading_symbol=parts[2] if len(parts) == 3 else None,
                )
            )
        return tuple(subscriptions)

    def assert_live_allowed(
        self, *, explicit_startup_confirmation: bool = False
    ) -> None:
        """Fail closed unless both live gates are explicitly satisfied."""
        if self.trading_mode is TradingMode.PAPER:
            return
        if not self.live_trading_enabled:
            raise RuntimeError("LIVE mode requested but LIVE_TRADING_ENABLED is false.")
        if not explicit_startup_confirmation:
            raise RuntimeError(
                "LIVE mode requested without explicit startup confirmation."
            )
