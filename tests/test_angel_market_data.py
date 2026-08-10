from __future__ import annotations

import logging
import ssl
import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from trading_bot.broker import (
    AngelCredentials,
    AngelOneMarketDataAdapter,
    BrokerAuthenticationError,
    MarketDataConnectionError,
    ReconnectPolicy,
)
from trading_bot.broker.angel import (
    VerifiedTLSSmartWebSocketV2,
    _SecretRedactionFilter,
    _verified_websocket_factory,
)
from trading_bot.domain import (
    IST,
    Exchange,
    FeedEvent,
    FeedEventType,
    MarketSubscription,
    SubscriptionMode,
)

FIXED_NOW = datetime(2026, 8, 10, 9, 30, 1, tzinfo=IST)
EXCHANGE_TIME = datetime(2026, 8, 10, 9, 30, tzinfo=IST)


class FakeRestClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.login_args: tuple[str, str, str] | None = None

    def generateSession(
        self, client_code: str, password: str, totp: str
    ) -> dict[str, Any]:
        self.login_args = (client_code, password, totp)
        return self.response

    def getfeedToken(self) -> str:
        return "feed-token-fallback"

    def nseIntraday(self) -> dict[str, Any]:
        return {
            "status": True,
            "data": [{"Exchange": "NSE", "SymbolName": "SBIN", "Multiplier": "5"}],
        }


class FakeWebSocket:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload
        self.on_open = lambda *_: None
        self.on_data = lambda *_: None
        self.on_error = lambda *_: None
        self.on_close = lambda *_: None
        self.subscription: tuple[str, int, list[dict[str, object]]] | None = None
        self.closed = False

    def subscribe(
        self,
        correlation_id: str,
        mode: int,
        token_list: list[dict[str, object]],
    ) -> None:
        self.subscription = (correlation_id, mode, token_list)

    def connect(self) -> None:
        self.on_open(self)
        if self.payload is not None:
            self.on_data(self, self.payload)

    def close_connection(self) -> None:
        self.closed = True
        self.on_close(self)


class ManualMonotonic:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _credentials() -> AngelCredentials:
    return AngelCredentials(
        api_key="api-key",
        client_code="client-code",
        password="pin",
        totp_secret="totp-seed",
    )


def _session_response() -> dict[str, Any]:
    return {
        "status": True,
        "data": {
            "jwtToken": "Bearer jwt-token",
            "refreshToken": "refresh-token",
            "feedToken": "feed-token",
        },
    }


def _payload() -> dict[str, Any]:
    return {
        "subscription_mode": 2,
        "exchange_type": 1,
        "token": "3045",
        "sequence_number": 42,
        "exchange_timestamp": int(EXCHANGE_TIME.astimezone(UTC).timestamp() * 1_000),
        "last_traded_price": 81_225,
        "last_traded_quantity": 5,
        "average_traded_price": 81_005,
        "volume_trade_for_the_day": 10_000,
        "total_buy_quantity": 50.5,
        "total_sell_quantity": 40.5,
        "open_price_of_the_day": 80_000,
        "high_price_of_the_day": 82_000,
        "low_price_of_the_day": 79_500,
        "closed_price": 80_300,
    }


def test_authentication_uses_totp_without_exposing_session_secrets() -> None:
    rest = FakeRestClient(_session_response())
    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        totp_provider=lambda secret: "123456" if secret == "totp-seed" else "wrong",
        clock=lambda: FIXED_NOW,
    )

    session = adapter.authenticate()

    assert rest.login_args == ("client-code", "pin", "123456")
    assert session.authenticated_at == FIXED_NOW
    assert "jwt-token" not in repr(session)
    assert "feed-token" not in repr(session)
    assert "client-code" not in repr(session)
    assert "api-key" not in repr(_credentials())


def test_authentication_fails_closed_on_rejected_response() -> None:
    rest = FakeRestClient({"status": False, "message": "bad credentials"})
    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        totp_provider=lambda _secret: "123456",
    )

    with pytest.raises(BrokerAuthenticationError, match="rejected"):
        adapter.authenticate()


def test_authenticated_intraday_reference_request_has_no_order_capability() -> None:
    rest = FakeRestClient(_session_response())
    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        totp_provider=lambda _secret: "123456",
        clock=lambda: FIXED_NOW,
    )

    response = adapter.fetch_nse_intraday()

    assert response["status"] is True
    assert not hasattr(adapter, "place_order")
    assert not hasattr(adapter, "cancel_order")


def test_sdk_log_filter_redacts_known_secrets_and_auth_headers() -> None:
    record = logging.LogRecord(
        name="smartapi",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=(
            "api-key pin client-code 123456 "
            "{'Authorization': 'Bearer dynamically-issued-jwt'}"
        ),
        args=(),
        exc_info=None,
    )
    log_filter = _SecretRedactionFilter(("api-key", "pin", "client-code", "123456"))

    assert log_filter.filter(record) is True
    rendered = record.getMessage()
    assert "api-key" not in rendered
    assert "client-code" not in rendered
    assert "dynamically-issued-jwt" not in rendered


def test_production_websocket_transport_requires_verified_tls(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeWebSocketApp:
        def __init__(self, url: str, **kwargs: object) -> None:
            captured["url"] = url
            captured["headers"] = kwargs["header"]

        def run_forever(self, **kwargs: object) -> None:
            captured["run_forever"] = kwargs

    monkeypatch.setattr(
        "trading_bot.broker.angel.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    transport = _verified_websocket_factory(
        "Bearer jwt",
        "api-key",
        "client-code",
        "feed-token",
    )

    assert isinstance(transport, VerifiedTLSSmartWebSocketV2)
    transport.connect()

    run_options = captured["run_forever"]
    assert isinstance(run_options, dict)
    ssl_options = run_options["sslopt"]
    assert isinstance(ssl_options, dict)
    assert ssl_options["cert_reqs"] == ssl.CERT_REQUIRED
    assert ssl_options["cert_reqs"] != ssl.CERT_NONE
    assert ssl_options["check_hostname"] is True


def test_normalizes_smartapi_paise_and_milliseconds() -> None:
    adapter = AngelOneMarketDataAdapter(_credentials(), clock=lambda: FIXED_NOW)

    tick = adapter.normalize_tick(_payload(), trading_symbol="SBIN-EQ")

    assert tick.exchange is Exchange.NSE_CM
    assert tick.subscription_mode is SubscriptionMode.QUOTE
    assert tick.exchange_timestamp == EXCHANGE_TIME
    assert tick.received_at == FIXED_NOW
    assert tick.last_traded_price == Decimal("812.25")
    assert tick.average_traded_price == Decimal("810.05")
    assert tick.open_price == Decimal("800")
    assert tick.total_buy_quantity == Decimal("50.5")


def test_currency_prices_use_the_smartapi_currency_divisor() -> None:
    adapter = AngelOneMarketDataAdapter(_credentials(), clock=lambda: FIXED_NOW)
    payload = _payload()
    payload["exchange_type"] = 13
    payload["last_traded_price"] = 835_125_000

    tick = adapter.normalize_tick(payload)

    assert tick.exchange is Exchange.CDE_FO
    assert tick.last_traded_price == Decimal("83.5125")


def test_stream_subscribes_records_tick_and_stops_cleanly() -> None:
    rest = FakeRestClient(_session_response())
    socket = FakeWebSocket(_payload())
    ticks = []
    events: list[FeedEvent] = []
    adapter: AngelOneMarketDataAdapter

    def handle_tick(tick: object) -> None:
        ticks.append(tick)
        adapter.disconnect()

    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        subscription_mode=SubscriptionMode.QUOTE,
        correlation_id="stream01",
        rest_factory=lambda _api_key: rest,
        websocket_factory=lambda *_: socket,
        totp_provider=lambda _secret: "123456",
        clock=lambda: FIXED_NOW,
    )
    subscription = MarketSubscription(Exchange.NSE_CM, "3045", "SBIN-EQ")

    adapter.stream([subscription], handle_tick, events.append)

    assert len(ticks) == 1
    assert ticks[0].trading_symbol == "SBIN-EQ"
    assert socket.subscription == (
        "stream01",
        2,
        [{"exchangeType": 1, "tokens": ["3045"]}],
    )
    assert socket.closed is True
    assert [event.event_type for event in events] == [
        FeedEventType.AUTHENTICATED,
        FeedEventType.CONNECTED,
        FeedEventType.SUBSCRIBED,
        FeedEventType.DISCONNECTED,
    ]
    assert not hasattr(adapter, "place_order")


def test_reconnect_attempts_are_bounded_with_capped_backoff() -> None:
    rest = FakeRestClient(_session_response())
    delays: list[float] = []
    connection_count = 0

    class FailingWebSocket(FakeWebSocket):
        def connect(self) -> None:
            nonlocal connection_count
            connection_count += 1
            raise OSError("network unavailable")

    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        websocket_factory=lambda *_: FailingWebSocket(),
        totp_provider=lambda _secret: "123456",
        reconnect_policy=ReconnectPolicy(
            max_attempts=3,
            initial_delay_seconds=1,
            multiplier=3,
            max_delay_seconds=4,
        ),
        sleeper=delays.append,
        clock=lambda: FIXED_NOW,
    )

    with pytest.raises(MarketDataConnectionError, match="bounded retries"):
        adapter.stream(
            [MarketSubscription(Exchange.NSE_CM, "3045")],
            lambda _tick: None,
        )

    assert connection_count == 4
    assert delays == [1, 3, 4]


def test_open_then_immediate_close_repeatedly_exhausts_retries() -> None:
    rest = FakeRestClient(_session_response())
    delays: list[float] = []
    connection_count = 0
    monotonic = ManualMonotonic()

    class FlappingWebSocket(FakeWebSocket):
        def connect(self) -> None:
            nonlocal connection_count
            connection_count += 1
            self.on_open(self)
            self.on_close(self)

    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        websocket_factory=lambda *_: FlappingWebSocket(),
        totp_provider=lambda _secret: "123456",
        reconnect_policy=ReconnectPolicy(
            max_attempts=2,
            initial_delay_seconds=1,
            multiplier=2,
            max_delay_seconds=5,
            stable_connection_seconds=30,
        ),
        monotonic_clock=monotonic,
        sleeper=delays.append,
        clock=lambda: FIXED_NOW,
    )

    with pytest.raises(MarketDataConnectionError, match="bounded retries"):
        adapter.stream(
            [MarketSubscription(Exchange.NSE_CM, "3045")],
            lambda _tick: None,
        )

    assert connection_count == 3
    assert delays == [1, 2]


def test_stable_connection_resets_prior_failure_streak() -> None:
    rest = FakeRestClient(_session_response())
    delays: list[float] = []
    monotonic = ManualMonotonic()
    sockets_created = 0
    adapter: AngelOneMarketDataAdapter

    class FailBeforeOpen(FakeWebSocket):
        def connect(self) -> None:
            raise OSError("handshake failed")

    class StableThenClose(FakeWebSocket):
        def connect(self) -> None:
            self.on_open(self)
            monotonic.advance(31)
            self.on_close(self)

    class UserDisconnect(FakeWebSocket):
        def connect(self) -> None:
            self.on_open(self)
            adapter.disconnect()

    def websocket_factory(*_: str) -> FakeWebSocket:
        nonlocal sockets_created
        sockets_created += 1
        if sockets_created == 1:
            return FailBeforeOpen()
        if sockets_created == 2:
            return StableThenClose()
        return UserDisconnect()

    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        websocket_factory=websocket_factory,
        totp_provider=lambda _secret: "123456",
        reconnect_policy=ReconnectPolicy(
            max_attempts=2,
            initial_delay_seconds=1,
            multiplier=2,
            max_delay_seconds=5,
            stable_connection_seconds=30,
        ),
        monotonic_clock=monotonic,
        sleeper=delays.append,
        clock=lambda: FIXED_NOW,
    )

    adapter.stream(
        [MarketSubscription(Exchange.NSE_CM, "3045")],
        lambda _tick: None,
    )

    assert sockets_created == 3
    assert delays == [1, 1]


def test_user_requested_disconnect_does_not_retry() -> None:
    rest = FakeRestClient(_session_response())
    delays: list[float] = []
    socket = FakeWebSocket(_payload())
    adapter: AngelOneMarketDataAdapter

    def stop_on_tick(_tick: object) -> None:
        adapter.disconnect()

    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        websocket_factory=lambda *_: socket,
        totp_provider=lambda _secret: "123456",
        sleeper=delays.append,
        clock=lambda: FIXED_NOW,
    )

    adapter.stream(
        [MarketSubscription(Exchange.NSE_CM, "3045")],
        stop_on_tick,
    )

    assert socket.closed is True
    assert delays == []


def test_smartapi_subscription_quota_accepts_1000() -> None:
    rest = FakeRestClient(_session_response())
    socket = FakeWebSocket()
    adapter: AngelOneMarketDataAdapter

    class DisconnectAfterOpen(FakeWebSocket):
        def connect(self) -> None:
            self.on_open(self)
            adapter.disconnect()

    socket = DisconnectAfterOpen()
    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        websocket_factory=lambda *_: socket,
        totp_provider=lambda _secret: "123456",
        clock=lambda: FIXED_NOW,
    )
    subscriptions = [
        MarketSubscription(Exchange.NSE_CM, str(token)) for token in range(1, 1_001)
    ]

    adapter.stream(subscriptions, lambda _tick: None)

    assert socket.subscription is not None
    assert len(socket.subscription[2][0]["tokens"]) == 1_000


def test_smartapi_subscription_quota_rejects_1001_before_authentication() -> None:
    rest_created = False
    socket_created = False

    def rest_factory(_api_key: str) -> FakeRestClient:
        nonlocal rest_created
        rest_created = True
        return FakeRestClient(_session_response())

    def websocket_factory(*_: str) -> FakeWebSocket:
        nonlocal socket_created
        socket_created = True
        return FakeWebSocket()

    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=rest_factory,
        websocket_factory=websocket_factory,
        totp_provider=lambda _secret: "123456",
        clock=lambda: FIXED_NOW,
    )
    subscriptions = [
        MarketSubscription(Exchange.NSE_CM, str(token)) for token in range(1, 1_002)
    ]

    with pytest.raises(ValueError, match="at most 1000"):
        adapter.stream(subscriptions, lambda _tick: None)

    assert rest_created is False
    assert socket_created is False


def test_adapter_rejects_a_second_concurrent_websocket() -> None:
    rest = FakeRestClient(_session_response())
    connected = threading.Event()
    release = threading.Event()

    class BlockingWebSocket(FakeWebSocket):
        def connect(self) -> None:
            self.on_open(self)
            connected.set()
            assert release.wait(1)

    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        websocket_factory=lambda *_: BlockingWebSocket(),
        totp_provider=lambda _secret: "123456",
        clock=lambda: FIXED_NOW,
    )
    subscription = [MarketSubscription(Exchange.NSE_CM, "3045")]
    first_error: list[BaseException] = []

    def run_first_stream() -> None:
        try:
            adapter.stream(subscription, lambda _tick: None)
        except BaseException as exc:
            first_error.append(exc)

    thread = threading.Thread(target=run_first_stream)
    thread.start()
    assert connected.wait(1)

    with pytest.raises(MarketDataConnectionError, match="only one"):
        adapter.stream(subscription, lambda _tick: None)

    adapter.disconnect()
    release.set()
    thread.join(1)
    assert not thread.is_alive()
    assert first_error == []


def test_event_recorder_failure_stops_before_connecting() -> None:
    rest = FakeRestClient(_session_response())
    socket_created = False

    def websocket_factory(*_: str) -> FakeWebSocket:
        nonlocal socket_created
        socket_created = True
        return FakeWebSocket()

    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        websocket_factory=websocket_factory,
        totp_provider=lambda _secret: "123456",
        clock=lambda: FIXED_NOW,
    )

    with pytest.raises(MarketDataConnectionError, match="event recorder failed"):
        adapter.stream(
            [MarketSubscription(Exchange.NSE_CM, "3045")],
            lambda _tick: None,
            lambda _event: (_ for _ in ()).throw(OSError("database unavailable")),
        )

    assert socket_created is False


def test_tick_recorder_failure_stops_and_surfaces_error() -> None:
    rest = FakeRestClient(_session_response())
    socket = FakeWebSocket(_payload())
    adapter = AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        websocket_factory=lambda *_: socket,
        totp_provider=lambda _secret: "123456",
        clock=lambda: FIXED_NOW,
    )

    def fail_to_record(_tick: object) -> None:
        raise OSError("database unavailable")

    with pytest.raises(MarketDataConnectionError, match="consumer failed"):
        adapter.stream(
            [MarketSubscription(Exchange.NSE_CM, "3045")],
            fail_to_record,
            lambda _event: None,
        )

    assert socket.closed is True
