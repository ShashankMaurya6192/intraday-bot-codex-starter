"""Environment-backed application configuration with fail-closed safety gates."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
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
    websocket_stable_connection_seconds: float = Field(default=30.0, gt=0, le=3600)

    tick_ingestion_queue_capacity: int = Field(default=10_000, ge=1, le=1_000_000)
    tick_ingestion_batch_size: int = Field(default=250, ge=1, le=10_000)
    tick_ingestion_flush_interval_seconds: float = Field(default=0.05, gt=0, le=5)
    tick_ingestion_shutdown_timeout_seconds: float = Field(default=10.0, gt=0, le=300)

    instrument_master_cache_path: Path = Path("data/cache/angel_instruments.json")
    instrument_master_max_age_days: int = Field(default=7, ge=1, le=90)
    intraday_universe_cache_path: Path = Path("data/cache/angel_nse_intraday.json")
    intraday_universe_max_age_seconds: float = Field(default=28_800, gt=0, le=86_400)
    nifty500_cache_path: Path = Path("data/cache/nifty500.json")
    nifty500_max_age_days: int = Field(default=7, ge=1, le=90)
    reference_download_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    reference_download_read_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    reference_download_max_attempts: int = Field(default=3, ge=1, le=5)

    scanner_symbols: str = ""
    nifty_benchmark_trading_symbol: str = "NIFTY 50"
    historical_bootstrap_lookback_days: int = Field(default=30, ge=22, le=30)
    historical_bootstrap_minimum_sessions: int = Field(default=15, ge=1, le=25)
    historical_request_delay_seconds: float = Field(default=0.5, ge=0.1, le=10)
    historical_request_max_attempts: int = Field(default=3, ge=1, le=5)
    historical_retry_initial_delay_seconds: float = Field(default=1.0, ge=0, le=60)
    historical_retry_max_delay_seconds: float = Field(default=8.0, ge=0, le=120)
    scan_replay_warning_tick_count: int = Field(default=500_000, ge=1)
    scan_replay_warning_seconds: float = Field(default=10.0, gt=0, le=3600)
    scan_replay_hard_tick_limit: int = Field(default=2_000_000, ge=1)
    market_data_recovery_consecutive_ticks: int = Field(default=3, ge=1, le=100)
    relative_volume_lookback_sessions: int = Field(default=15, ge=1, le=100)
    relative_volume_minimum_sessions: int = Field(default=10, ge=1, le=100)
    relative_volume_threshold: Decimal = Field(default=Decimal("1.5"), gt=0)
    opening_range_minutes: int = Field(default=15, ge=1, le=60)
    scanner_breakout_proximity_pct: Decimal = Field(
        default=Decimal("0.25"), ge=0, le=10
    )
    regime_momentum_threshold_pct: Decimal = Field(default=Decimal("0.05"), ge=0, le=10)
    regime_slope_lookback_candles: int = Field(default=3, ge=1, le=20)
    scanner_weight_vwap: Decimal = Field(default=Decimal("2"), ge=0)
    scanner_weight_ema: Decimal = Field(default=Decimal("2"), ge=0)
    scanner_weight_relative_volume: Decimal = Field(default=Decimal("2"), ge=0)
    scanner_weight_relative_strength: Decimal = Field(default=Decimal("2"), ge=0)
    scanner_weight_opening_range: Decimal = Field(default=Decimal("1"), ge=0)
    scanner_weight_market_regime: Decimal = Field(default=Decimal("1"), ge=0)

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
            raise ValueError("only sqlite:/// database URLs are supported in Phase 2")
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

    @field_validator("nifty_benchmark_trading_symbol")
    @classmethod
    def validate_benchmark_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("nifty benchmark trading symbol must not be blank")
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
        if self.tick_ingestion_batch_size > self.tick_ingestion_queue_capacity:
            raise ValueError("tick ingestion batch size cannot exceed queue capacity")
        if (
            self.tick_ingestion_flush_interval_seconds
            > self.tick_ingestion_shutdown_timeout_seconds
        ):
            raise ValueError(
                "tick ingestion flush interval cannot exceed shutdown timeout"
            )
        if (
            self.relative_volume_minimum_sessions
            > self.relative_volume_lookback_sessions
        ):
            raise ValueError(
                "relative volume minimum sessions cannot exceed lookback sessions"
            )
        if (
            self.historical_bootstrap_minimum_sessions
            > self.historical_bootstrap_lookback_days
        ):
            raise ValueError(
                "historical minimum sessions cannot exceed lookback calendar days"
            )
        if (
            self.historical_retry_initial_delay_seconds
            > self.historical_retry_max_delay_seconds
        ):
            raise ValueError(
                "historical retry initial delay cannot exceed maximum delay"
            )
        if self.scan_replay_warning_tick_count > self.scan_replay_hard_tick_limit:
            raise ValueError(
                "scan replay warning tick count cannot exceed the hard tick limit"
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

    def configured_scanner_symbols(self) -> tuple[str, ...]:
        if not self.scanner_symbols.strip():
            return ()
        symbols = tuple(
            symbol.strip().upper()
            for symbol in self.scanner_symbols.split(",")
            if symbol.strip()
        )
        if len(symbols) != len(set(symbols)):
            raise ValueError("SCANNER_SYMBOLS must not contain duplicates")
        return symbols

    def assert_live_allowed(
        self, *, explicit_startup_confirmation: bool = False
    ) -> None:
        """Fail closed unless both future live gates are explicitly satisfied."""
        if self.trading_mode is TradingMode.PAPER:
            return
        if not self.live_trading_enabled:
            raise RuntimeError("LIVE mode requested but LIVE_TRADING_ENABLED is false.")
        if not explicit_startup_confirmation:
            raise RuntimeError(
                "LIVE mode requested without explicit startup confirmation."
            )
