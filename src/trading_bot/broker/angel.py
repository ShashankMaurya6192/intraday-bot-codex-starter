"""Angel One SmartAPI authentication and WebSocket V2 market-data adapter."""

from __future__ import annotations

import logging
import re
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Protocol, cast

import websocket
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from trading_bot.domain import (
    IST,
    Exchange,
    FeedEvent,
    FeedEventType,
    MarketSubscription,
    MarketTick,
    SubscriptionMode,
)

from .interfaces import (
    BrokerAuthenticationError,
    BrokerReferenceDataError,
    BrokerSession,
    FeedEventHandler,
    MarketDataBroker,
    MarketDataConnectionError,
    ReconnectPolicy,
    TickHandler,
    TickNormalizationError,
)

_EXCHANGE_TO_SMARTAPI = {
    Exchange.NSE_CM: 1,
    Exchange.NSE_FO: 2,
    Exchange.BSE_CM: 3,
    Exchange.BSE_FO: 4,
    Exchange.MCX_FO: 5,
    Exchange.NCX_FO: 7,
    Exchange.CDE_FO: 13,
}
_SMARTAPI_TO_EXCHANGE = {value: key for key, value in _EXCHANGE_TO_SMARTAPI.items()}

_MODE_TO_SMARTAPI = {
    SubscriptionMode.LTP: 1,
    SubscriptionMode.QUOTE: 2,
    SubscriptionMode.SNAP_QUOTE: 3,
    SubscriptionMode.DEPTH: 4,
}
_SMARTAPI_TO_MODE = {value: key for key, value in _MODE_TO_SMARTAPI.items()}

_PAISE_PER_RUPEE = Decimal("100")
_CURRENCY_PRICE_DIVISOR = Decimal("10000000")
_SUPPORTED_SMARTAPI_SDK_VERSION = "1.5.5"
_MAX_TOKEN_MODE_SUBSCRIPTIONS = 1_000
_SENSITIVE_HEADER_PATTERN = re.compile(
    r"(?i)((?:authorization|x-api-key|x-client-code|x-feed-token|"
    r"x-privatekey)['\"]?\s*[:=]\s*['\"]?)(?:bearer\s+)?[^'\"\s,}]+"
)


class _RestClient(Protocol):
    def generateSession(
        self, client_code: str, password: str, totp: str
    ) -> Mapping[str, Any]: ...

    def getfeedToken(self) -> str: ...

    def nseIntraday(self) -> Mapping[str, Any]: ...

    def getCandleData(
        self, historicDataParams: Mapping[str, object]
    ) -> Mapping[str, Any]: ...


class _WebSocketClient(Protocol):
    on_open: Callable[..., None]
    on_data: Callable[..., None]
    on_error: Callable[..., None]
    on_close: Callable[..., None]

    def subscribe(
        self,
        correlation_id: str,
        mode: int,
        token_list: list[dict[str, object]],
    ) -> None: ...

    def connect(self) -> None: ...

    def close_connection(self) -> None: ...


RestFactory = Callable[[str], _RestClient]
WebSocketFactory = Callable[[str, str, str, str], _WebSocketClient]
TotpProvider = Callable[[str], str]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
MonotonicClock = Callable[[], float]


class _SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self._secrets = tuple(
            sorted(
                {secret for secret in secrets if secret},
                key=len,
                reverse=True,
            )
        )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "***")
        message = _SENSITIVE_HEADER_PATTERN.sub(r"\1***", message)
        record.msg = message
        record.args = ()
        return True


@dataclass(frozen=True, slots=True)
class AngelCredentials:
    api_key: str
    client_code: str
    password: str
    totp_secret: str

    def __post_init__(self) -> None:
        for field_name in ("api_key", "client_code", "password", "totp_secret"):
            value = getattr(self, field_name).strip()
            if not value:
                raise ValueError(f"Angel One {field_name} must not be blank")
            object.__setattr__(self, field_name, value)

    def __repr__(self) -> str:
        return (
            "AngelCredentials(api_key=***, client_code=***, "
            "password=***, totp_secret=***)"
        )


class VerifiedTLSSmartWebSocketV2(SmartWebSocketV2):
    """SmartAPI 1.5.5 transport with certificate verification restored.

    The pinned SDK's parser, subscription logic, heartbeat handling, and callback
    adapters are inherited unchanged. Only ``connect`` is replaced because the
    upstream implementation explicitly passes ``ssl.CERT_NONE``.
    """

    def connect(self) -> None:
        headers = {
            "Authorization": self.auth_token,
            "x-api-key": self.api_key,
            "x-client-code": self.client_code,
            "x-feed-token": self.feed_token,
        }
        self.wsapp = websocket.WebSocketApp(
            self.ROOT_URI,
            header=headers,
            on_open=self._on_open,
            on_error=self._on_error,
            on_close=self._on_close,
            on_data=self._on_data,
            on_ping=self._on_ping,
            on_pong=self._on_pong,
        )
        self.wsapp.run_forever(
            sslopt={
                "cert_reqs": ssl.CERT_REQUIRED,
                "check_hostname": True,
            },
            ping_interval=self.HEART_BEAT_INTERVAL,
        )


def _official_rest_factory(api_key: str) -> _RestClient:
    from SmartApi import SmartConnect

    return cast(_RestClient, SmartConnect(api_key=api_key))


def _verified_websocket_factory(
    access_token: str, api_key: str, client_code: str, feed_token: str
) -> _WebSocketClient:
    try:
        installed_version = version("smartapi-python")
    except PackageNotFoundError as exc:
        raise MarketDataConnectionError(
            "the pinned smartapi-python SDK is not installed"
        ) from exc
    if installed_version != _SUPPORTED_SMARTAPI_SDK_VERSION:
        raise MarketDataConnectionError(
            "unsupported smartapi-python version for the verified TLS wrapper"
        )

    # Reconnection is managed by this adapter so retry timing is bounded and testable.
    return cast(
        _WebSocketClient,
        VerifiedTLSSmartWebSocketV2(
            access_token,
            api_key,
            client_code,
            feed_token,
            max_retry_attempt=0,
        ),
    )


def _official_totp_provider(secret: str) -> str:
    import pyotp

    return pyotp.TOTP(secret).now()


class AngelOneMarketDataAdapter(MarketDataBroker):
    """Official SDK wrapper containing no order-placement capability."""

    def __init__(
        self,
        credentials: AngelCredentials,
        *,
        subscription_mode: SubscriptionMode = SubscriptionMode.QUOTE,
        correlation_id: str = "intraday01",
        reconnect_policy: ReconnectPolicy | None = None,
        rest_factory: RestFactory = _official_rest_factory,
        websocket_factory: WebSocketFactory = _verified_websocket_factory,
        totp_provider: TotpProvider = _official_totp_provider,
        clock: Clock | None = None,
        monotonic_clock: MonotonicClock = time.monotonic,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        if not 1 <= len(correlation_id) <= 10 or not correlation_id.isalnum():
            raise ValueError("correlation_id must be 1-10 alphanumeric characters")
        if subscription_mode is SubscriptionMode.DEPTH:
            # SmartAPI depth has tighter exchange/token limits. It is deliberately
            # excluded from the initial equity recorder until separately tested.
            raise ValueError("DEPTH mode is not supported by the Phase 2 adapter")

        self._credentials = credentials
        self._subscription_mode = subscription_mode
        self._correlation_id = correlation_id
        self._reconnect_policy = reconnect_policy or ReconnectPolicy()
        self._rest_factory = rest_factory
        self._websocket_factory = websocket_factory
        self._totp_provider = totp_provider
        self._clock = clock or (lambda: datetime.now(IST))
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper

        self._session: BrokerSession | None = None
        self._rest_client: _RestClient | None = None
        self._socket: _WebSocketClient | None = None
        self._socket_lock = threading.RLock()
        self._stream_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._callback_failure: Exception | None = None
        self._event_handler: FeedEventHandler | None = None
        self._connection_opened_at: float | None = None
        self._sdk_log_filter: _SecretRedactionFilter | None = None

    @property
    def session(self) -> BrokerSession | None:
        return self._session

    def authenticate(self) -> BrokerSession:
        try:
            totp = self._totp_provider(self._credentials.totp_secret)
            client = self._rest_factory(self._credentials.api_key)
            self._install_sdk_log_redaction(totp)
            response = client.generateSession(
                self._credentials.client_code,
                self._credentials.password,
                totp,
            )
        except Exception as exc:
            raise BrokerAuthenticationError(
                "Angel One authentication failed; no session was created"
            ) from exc

        if not isinstance(response, Mapping) or response.get("status") is not True:
            raise BrokerAuthenticationError(
                "Angel One rejected authentication; inspect the broker portal "
                "and credentials"
            )
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise BrokerAuthenticationError(
                "Angel One authentication returned no session data"
            )

        access_token = _nonblank_string(data.get("jwtToken"))
        refresh_token = _nonblank_string(data.get("refreshToken"))
        feed_token = _nonblank_string(data.get("feedToken"))
        if feed_token is None:
            try:
                feed_token = _nonblank_string(client.getfeedToken())
            except Exception as exc:
                raise BrokerAuthenticationError(
                    "Angel One session did not provide a feed token"
                ) from exc

        if access_token is None or refresh_token is None or feed_token is None:
            raise BrokerAuthenticationError(
                "Angel One authentication returned an incomplete session"
            )

        authenticated_at = self._aware_now()
        session = BrokerSession(
            client_code=self._credentials.client_code,
            authenticated_at=authenticated_at,
            access_token=access_token,
            refresh_token=refresh_token,
            feed_token=feed_token,
        )
        self._rest_client = client
        self._session = session
        return session

    def fetch_nse_intraday(self) -> Mapping[str, Any]:
        """Fetch Angel's authenticated analysis-only NSE intraday universe."""
        if self._session is None or self._rest_client is None:
            self.authenticate()
        client = self._rest_client
        if client is None:
            raise BrokerReferenceDataError("Angel REST session is unavailable")
        try:
            response = client.nseIntraday()
        except Exception as exc:
            raise BrokerReferenceDataError(
                "Angel NSE intraday-universe request failed"
            ) from exc
        if not isinstance(response, Mapping):
            raise BrokerReferenceDataError(
                "Angel NSE intraday-universe response is malformed"
            )
        return response

    def fetch_historical_candles(
        self, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Call the official SDK's authenticated historical POST endpoint."""
        if self._session is None or self._rest_client is None:
            self.authenticate()
        client = self._rest_client
        if client is None:
            raise BrokerReferenceDataError("Angel REST session is unavailable")
        try:
            # SmartAPI mutates its parameter dictionary while removing None values.
            response = client.getCandleData(dict(parameters))
        except Exception as exc:
            raise BrokerReferenceDataError(
                "Angel historical-candle request failed"
            ) from exc
        if not isinstance(response, Mapping):
            raise BrokerReferenceDataError(
                "Angel historical-candle response is malformed"
            )
        return response

    def stream(
        self,
        subscriptions: Sequence[MarketSubscription],
        on_tick: TickHandler,
        on_event: FeedEventHandler | None = None,
    ) -> None:
        if not self._stream_lock.acquire(blocking=False):
            raise MarketDataConnectionError(
                "only one SmartAPI WebSocket connection is supported per adapter"
            )
        try:
            self._run_stream(subscriptions, on_tick, on_event)
        finally:
            self._stream_lock.release()

    def _run_stream(
        self,
        subscriptions: Sequence[MarketSubscription],
        on_tick: TickHandler,
        on_event: FeedEventHandler | None,
    ) -> None:
        subscriptions = tuple(subscriptions)
        if not subscriptions:
            raise ValueError("at least one market-data subscription is required")
        if len(subscriptions) > _MAX_TOKEN_MODE_SUBSCRIPTIONS:
            raise ValueError(
                "SmartAPI supports at most 1000 token-mode subscriptions "
                "per WebSocket connection"
            )
        if len({(item.exchange, item.symbol_token) for item in subscriptions}) != len(
            subscriptions
        ):
            raise ValueError("duplicate market-data subscriptions are not allowed")

        self._event_handler = on_event
        self._stop_requested.clear()
        self._callback_failure = None

        if self._session is None:
            try:
                self.authenticate()
            except BrokerAuthenticationError:
                self._emit(
                    FeedEventType.ERROR,
                    "Angel One market-data authentication failed",
                )
                raise
        self._emit(
            FeedEventType.AUTHENTICATED,
            "Angel One market-data session authenticated",
        )

        session = self._session
        if session is None:  # Defensive guard for future authentication changes.
            raise BrokerAuthenticationError("Angel One session is unavailable")

        lookup = {
            (item.exchange, item.symbol_token): item.trading_symbol
            for item in subscriptions
        }
        token_list = self._build_token_list(subscriptions)
        failure_streak = 0
        last_error: Exception | None = None

        while not self._stop_requested.is_set():
            self._connection_opened_at = None
            try:
                socket = self._websocket_factory(
                    session.access_token,
                    self._credentials.api_key,
                    session.client_code,
                    session.feed_token,
                )
                with self._socket_lock:
                    self._socket = socket
                self._bind_callbacks(socket, token_list, lookup, on_tick)
                socket.connect()
                last_error = None
            except KeyboardInterrupt:
                self.disconnect()
                raise
            except Exception as exc:
                last_error = exc
            finally:
                with self._socket_lock:
                    self._socket = None

            if self._callback_failure is not None:
                raise MarketDataConnectionError(
                    "market-data consumer failed; recording stopped to avoid data gaps"
                ) from self._callback_failure
            if self._stop_requested.is_set():
                return

            connection_duration: float | None = None
            stable_connection = False
            if self._connection_opened_at is not None:
                connection_duration = max(
                    0.0,
                    self._monotonic_clock() - self._connection_opened_at,
                )
                stable_connection = (
                    connection_duration
                    >= self._reconnect_policy.stable_connection_seconds
                )
                if stable_connection:
                    failure_streak = 0

            if failure_streak >= self._reconnect_policy.max_attempts:
                self._emit(
                    FeedEventType.ERROR,
                    "Angel One market-data reconnect attempts exhausted",
                    {"attempts": failure_streak},
                )
                raise MarketDataConnectionError(
                    "Angel One market-data stream stopped after bounded retries"
                ) from last_error

            failure_streak += 1
            delay = self._reconnect_policy.delay_for_attempt(failure_streak)
            self._emit(
                FeedEventType.RECONNECTING,
                "Reconnecting Angel One market-data stream",
                {
                    "attempt": failure_streak,
                    "delay_seconds": delay,
                    "connection_duration_seconds": connection_duration,
                    "stable_connection": stable_connection,
                },
            )
            self._sleeper(delay)

    def disconnect(self) -> None:
        self._stop_requested.set()
        with self._socket_lock:
            socket = self._socket
        if socket is not None:
            try:
                socket.close_connection()
            except Exception:
                # The stop flag is authoritative even if the SDK close call fails.
                pass

    def normalize_tick(
        self,
        payload: Mapping[str, Any],
        *,
        trading_symbol: str | None = None,
    ) -> MarketTick:
        """Convert the official SDK's paise/millisecond payload into a domain tick."""
        try:
            exchange = _SMARTAPI_TO_EXCHANGE[_required_int(payload, "exchange_type")]
            subscription_mode = _SMARTAPI_TO_MODE[
                _required_int(payload, "subscription_mode")
            ]
            exchange_timestamp_ms = _required_int(payload, "exchange_timestamp")
            if exchange_timestamp_ms <= 0:
                raise ValueError("exchange timestamp must be positive")
            exchange_timestamp = datetime.fromtimestamp(
                exchange_timestamp_ms / 1_000, tz=UTC
            ).astimezone(IST)
            return MarketTick(
                exchange=exchange,
                symbol_token=_required_string(payload, "token"),
                trading_symbol=trading_symbol,
                subscription_mode=subscription_mode,
                sequence_number=_required_int(payload, "sequence_number"),
                exchange_timestamp=exchange_timestamp,
                received_at=self._aware_now(),
                last_traded_price=_price(
                    payload, "last_traded_price", exchange, required=True
                ),
                last_traded_quantity=_optional_int(payload, "last_traded_quantity"),
                average_traded_price=_price(payload, "average_traded_price", exchange),
                volume_trade_for_the_day=_optional_int(
                    payload, "volume_trade_for_the_day"
                ),
                total_buy_quantity=_optional_decimal(payload, "total_buy_quantity"),
                total_sell_quantity=_optional_decimal(payload, "total_sell_quantity"),
                open_price=_price(payload, "open_price_of_the_day", exchange),
                high_price=_price(payload, "high_price_of_the_day", exchange),
                low_price=_price(payload, "low_price_of_the_day", exchange),
                close_price=_price(payload, "closed_price", exchange),
                raw_payload=payload,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise TickNormalizationError(
                "invalid Angel One market-data payload"
            ) from exc

    def _bind_callbacks(
        self,
        socket: _WebSocketClient,
        token_list: list[dict[str, object]],
        symbol_lookup: Mapping[tuple[Exchange, str], str | None],
        on_tick: TickHandler,
    ) -> None:
        def on_open(*_: object) -> None:
            self._connection_opened_at = self._monotonic_clock()
            self._emit(
                FeedEventType.CONNECTED,
                "Angel One market-data WebSocket connected",
            )
            socket.subscribe(
                self._correlation_id,
                _MODE_TO_SMARTAPI[self._subscription_mode],
                token_list,
            )
            self._emit(
                FeedEventType.SUBSCRIBED,
                "Angel One market-data subscriptions active",
                {"subscription_count": sum(len(item["tokens"]) for item in token_list)},
            )

        def on_data(_wsapp: object, payload: object, *_: object) -> None:
            if not isinstance(payload, Mapping):
                self._emit(
                    FeedEventType.ERROR,
                    "Ignored malformed Angel One market-data payload",
                    {"reason": "payload_not_mapping"},
                )
                return
            try:
                exchange = _SMARTAPI_TO_EXCHANGE[
                    _required_int(payload, "exchange_type")
                ]
                token = _required_string(payload, "token")
                tick = self.normalize_tick(
                    payload,
                    trading_symbol=symbol_lookup.get((exchange, token)),
                )
            except TickNormalizationError:
                self._emit(
                    FeedEventType.ERROR,
                    "Ignored invalid Angel One market-data tick",
                    {"reason": "normalization_failed"},
                )
                return

            try:
                on_tick(tick)
            except Exception as exc:
                self._callback_failure = exc
                try:
                    self._emit(
                        FeedEventType.ERROR,
                        "Market-data consumer failed; stopping the feed",
                        {"error_type": type(exc).__name__},
                    )
                finally:
                    self.disconnect()

        def on_error(*args: object) -> None:
            error = args[-1] if args else None
            error_type = type(error).__name__ if error is not None else "Unknown"
            self._emit(
                FeedEventType.ERROR,
                "Angel One market-data WebSocket reported an error",
                {"error_type": error_type},
            )

        def on_close(*_: object) -> None:
            self._emit(
                FeedEventType.DISCONNECTED,
                "Angel One market-data WebSocket disconnected",
                {"requested": self._stop_requested.is_set()},
            )

        socket.on_open = on_open
        socket.on_data = on_data
        socket.on_error = on_error
        socket.on_close = on_close

    def _build_token_list(
        self, subscriptions: Sequence[MarketSubscription]
    ) -> list[dict[str, object]]:
        grouped: dict[int, list[str]] = {}
        for subscription in subscriptions:
            exchange_type = _EXCHANGE_TO_SMARTAPI[subscription.exchange]
            grouped.setdefault(exchange_type, []).append(subscription.symbol_token)
        return [
            {"exchangeType": exchange_type, "tokens": tokens}
            for exchange_type, tokens in sorted(grouped.items())
        ]

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(IST)

    def _install_sdk_log_redaction(self, totp: str) -> None:
        if self._sdk_log_filter is not None:
            return
        import logzero

        log_filter = _SecretRedactionFilter(
            (
                self._credentials.api_key,
                self._credentials.client_code,
                self._credentials.password,
                self._credentials.totp_secret,
                totp,
            )
        )
        logzero.logger.addFilter(log_filter)
        self._sdk_log_filter = log_filter

    def _emit(
        self,
        event_type: FeedEventType,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if self._event_handler is None:
            return
        event = FeedEvent(
            event_type=event_type,
            occurred_at=self._aware_now(),
            message=message,
            details=details or {},
        )
        try:
            self._event_handler(event)
        except Exception as exc:
            self._callback_failure = exc
            self.disconnect()
            raise MarketDataConnectionError(
                "market-data event recorder failed; feed stopped"
            ) from exc


def _nonblank_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = _nonblank_string(payload.get(key))
    if value is None:
        raise ValueError(f"{key} is required")
    return value


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    if key not in payload or isinstance(payload[key], bool):
        raise ValueError(f"{key} is required")
    return int(payload[key])


def _optional_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return int(value)


def _optional_decimal(payload: Mapping[str, Any], key: str) -> Decimal | None:
    value = payload.get(key)
    if value is None:
        return None
    return Decimal(str(value))


def _price(
    payload: Mapping[str, Any],
    key: str,
    exchange: Exchange,
    *,
    required: bool = False,
) -> Decimal | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise ValueError(f"{key} is required")
        return None
    divisor = (
        _CURRENCY_PRICE_DIVISOR if exchange is Exchange.CDE_FO else _PAISE_PER_RUPEE
    )
    return Decimal(str(value)) / divisor
