from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.broker import (
    AngelCredentials,
    AngelOneMarketDataAdapter,
    BrokerAuthenticationError,
    BrokerHistoricalRateLimitError,
    BrokerHistoricalResponseError,
    BrokerHistoricalTransportError,
    BrokerInstrument,
)
from trading_bot.domain import IST, Exchange
from trading_bot.marketdata import (
    HistoricalBootstrapService,
    run_historical_diagnostic,
)
from trading_bot.storage import SQLiteMarketRecorder

SESSION_DATE = date(2026, 8, 7)
NOW = datetime(2026, 8, 10, 8, 0, tzinfo=IST)


def _credentials() -> AngelCredentials:
    return AngelCredentials(
        api_key="api-key-secret",
        client_code="client-code-secret",
        password="pin-secret",
        totp_secret="totp-secret",
    )


def _session_response() -> dict[str, object]:
    return {
        "status": True,
        "data": {
            "jwtToken": "jwt-secret",
            "refreshToken": "refresh-secret",
            "feedToken": "feed-secret",
        },
    }


class _RestClient:
    def __init__(self, historical: object) -> None:
        self.historical = historical
        self.calls = 0

    def generateSession(self, *_args: object) -> dict[str, object]:
        return _session_response()

    def getfeedToken(self) -> str:
        return "feed-secret"

    def getCandleData(self, _parameters: object) -> dict[str, object]:
        self.calls += 1
        if isinstance(self.historical, Exception):
            raise self.historical
        assert isinstance(self.historical, dict)
        return self.historical


class _SdkDataException(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _adapter(rest: _RestClient) -> AngelOneMarketDataAdapter:
    return AngelOneMarketDataAdapter(
        _credentials(),
        rest_factory=lambda _api_key: rest,
        totp_provider=lambda _secret: "123456",
        clock=lambda: NOW,
    )


def _instrument(token: str, symbol: str) -> BrokerInstrument:
    return BrokerInstrument(
        Exchange.NSE_CM,
        token,
        symbol,
        Decimal("0.05"),
    )


def _benchmark() -> BrokerInstrument:
    return BrokerInstrument(
        Exchange.NSE_CM,
        "99926000",
        "Z NIFTY 50",
        Decimal("0"),
        instrument_type="AMXIDX",
    )


def _historical_response(
    session_date: date = SESSION_DATE, *, start: str = "100"
) -> dict[str, object]:
    session_open = datetime.combine(
        session_date, datetime.min.time(), tzinfo=IST
    ).replace(hour=9, minute=15)
    base = Decimal(start)
    rows: list[list[object]] = []
    for minute in range(375):
        close = base + Decimal(minute) / Decimal("100")
        rows.append(
            [
                (session_open + timedelta(minutes=minute)).isoformat(),
                str(close),
                str(close + Decimal("0.05")),
                str(close - Decimal("0.05")),
                str(close),
                10,
            ]
        )
    return {
        "status": True,
        "message": "SUCCESS",
        "errorcode": "",
        "data": rows,
    }


def _service(
    broker: object, recorder: SQLiteMarketRecorder
) -> HistoricalBootstrapService:
    return HistoricalBootstrapService(
        broker,  # type: ignore[arg-type]
        recorder,
        lookback_days=22,
        minimum_sessions=1,
        request_delay_seconds=0,
        max_attempts=3,
        retry_initial_delay_seconds=0,
        retry_max_delay_seconds=0,
        sleeper=lambda _delay: None,
    )


def test_sdk_non_json_rate_limit_is_classified_and_redacted() -> None:
    exception = _SdkDataException(
        "Couldn't parse the JSON response received from the server: "
        "b'Access denied because of exceeding access rate' "
        "Authorization: Bearer jwt-secret api-key-secret client-code-secret",
        code=502,
    )
    rest = _RestClient(exception)

    with pytest.raises(BrokerHistoricalRateLimitError) as raised:
        _adapter(rest).fetch_historical_candles(
            {"exchange": "NSE", "symboltoken": "1"}
        )

    assert rest.calls == 1
    rendered = json.dumps(
        {
            "reason": str(raised.value),
            "broker_message": raised.value.broker_message,
            "error_code": raised.value.error_code,
        }
    )
    for secret in (
        "api-key-secret",
        "client-code-secret",
        "pin-secret",
        "totp-secret",
        "jwt-secret",
        "refresh-secret",
        "feed-secret",
    ):
        assert secret not in rendered
    assert "exceeding access rate" in rendered


def test_http_403_with_unambiguous_response_body_is_rate_limited() -> None:
    class Response:
        status_code = 403
        text = "Access denied because of exceeding access rate"
        content = None

    exception = RuntimeError("HTTP request rejected")
    exception.response = Response()  # type: ignore[attr-defined]

    with pytest.raises(BrokerHistoricalRateLimitError) as raised:
        _adapter(_RestClient(exception)).fetch_historical_candles(
            {"exchange": "NSE", "symboltoken": "1"}
        )

    assert raised.value.broker_status == 403
    assert raised.value.broker_message == (
        "HTTP request rejected Access denied because of exceeding access rate"
    )


def test_generic_network_and_rejected_responses_are_not_rate_limits() -> None:
    with pytest.raises(BrokerHistoricalTransportError, match="connection reset"):
        adapter = _adapter(_RestClient(OSError("connection reset by peer")))
        adapter.fetch_historical_candles({"exchange": "NSE", "symboltoken": "1"})

    rejected = _RestClient(
        {"status": False, "message": "Invalid interval", "errorcode": "AB1234"}
    )
    with pytest.raises(BrokerHistoricalResponseError) as raised:
        _adapter(rejected).fetch_historical_candles(
            {"exchange": "NSE", "symboltoken": "1"}
        )
    assert not isinstance(raised.value, BrokerHistoricalRateLimitError)
    assert raised.value.broker_message == "Invalid interval"
    assert raised.value.error_code == "AB1234"


def test_authentication_failure_remains_an_authentication_failure() -> None:
    class RejectedAuthentication(_RestClient):
        def generateSession(self, *_args: object) -> dict[str, object]:
            raise RuntimeError("Access denied because of exceeding access rate")

    with pytest.raises(BrokerAuthenticationError) as raised:
        _adapter(RejectedAuthentication(_historical_response())).fetch_historical_candles(
            {"exchange": "NSE", "symboltoken": "1"}
        )
    assert not isinstance(raised.value, BrokerHistoricalRateLimitError)


class _BatchBroker:
    def __init__(self, outcomes: dict[str, object]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def fetch_historical_candles(self, parameters):
        token = str(parameters["symboltoken"])
        self.calls.append(token)
        outcome = self.outcomes[token]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _throttle() -> BrokerHistoricalRateLimitError:
    return BrokerHistoricalRateLimitError(
        "Angel Historical API rate limit: "
        "Access denied because of exceeding access rate",
        broker_status=False,
        broker_message="Access denied because of exceeding access rate",
    )


def test_throttle_on_first_symbol_aborts_batch_without_retries_or_later_calls() -> None:
    instruments = (_instrument("1", "A-EQ"), _instrument("2", "B-EQ"))
    broker = _BatchBroker(
        {
            "1": _throttle(),
            "2": _historical_response(),
            "99926000": _historical_response(),
        }
    )
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        result = _service(broker, recorder).bootstrap(
            instruments, benchmark=_benchmark(), now=NOW
        )

    assert broker.calls == ["1"]
    assert result.status == "ABORTED_RATE_LIMIT"
    assert result.reason_category == "RATE_LIMITED"
    assert result.rate_limited_symbol == "A-EQ"
    assert result.completed_symbols == 0
    assert result.failed_symbols == 1
    assert result.skipped_after_abort == 2
    payload = result.to_dict()
    assert payload["order_placement_available"] is False
    assert {
        "status",
        "requested",
        "completed",
        "failed",
        "skipped_after_abort",
        "rate_limited_symbol",
        "reason_category",
        "operator_action",
    } <= payload.keys()


def test_throttle_after_two_symbols_persists_work_and_rerun_resumes() -> None:
    instruments = (
        _instrument("1", "A-EQ"),
        _instrument("2", "B-EQ"),
        _instrument("3", "C-EQ"),
    )
    first_broker = _BatchBroker(
        {
            "1": _historical_response(start="100"),
            "2": _historical_response(start="200"),
            "3": _throttle(),
            "99926000": _historical_response(start="20000"),
        }
    )
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        first = _service(first_broker, recorder).bootstrap(
            instruments, benchmark=_benchmark(), now=NOW
        )
        assert first_broker.calls == ["1", "2", "3"]
        assert first.completed_symbols == 2
        assert first.skipped_after_abort == 1
        assert recorder.load_historical_bootstrap_status(
            exchange=Exchange.NSE_CM, symbol_token="1"
        ) is not None
        assert recorder.load_historical_bootstrap_status(
            exchange=Exchange.NSE_CM, symbol_token="2"
        ) is not None
        assert recorder.load_historical_bootstrap_status(
            exchange=Exchange.NSE_CM, symbol_token="3"
        ) is None

        resumed_broker = _BatchBroker(
            {
                "1": _historical_response(start="100"),
                "2": _historical_response(start="200"),
                "3": _historical_response(start="300"),
                "99926000": _historical_response(start="20000"),
            }
        )
        resumed = _service(resumed_broker, recorder).bootstrap(
            instruments, benchmark=_benchmark(), now=NOW
        )

    assert resumed_broker.calls == ["3", "99926000"]
    assert resumed.status == "COMPLETED"
    assert resumed.completed_symbols == 4
    assert resumed.failed_symbols == 0


def test_force_refresh_still_aborts_and_preserves_existing_dataset() -> None:
    instrument = _instrument("1", "A-EQ")
    successful_broker = _BatchBroker(
        {"1": _historical_response(), "99926000": _historical_response(start="20000")}
    )
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        completed = _service(successful_broker, recorder).bootstrap(
            (instrument,), benchmark=_benchmark(), now=NOW
        )
        assert completed.status == "COMPLETED"
        previous = recorder.load_historical_bootstrap_status(
            exchange=Exchange.NSE_CM, symbol_token="1"
        )

        throttled_broker = _BatchBroker(
            {"1": _throttle(), "99926000": _historical_response(start="20000")}
        )
        aborted = _service(throttled_broker, recorder).bootstrap(
            (instrument,), benchmark=_benchmark(), now=NOW, force=True
        )
        current = recorder.load_historical_bootstrap_status(
            exchange=Exchange.NSE_CM, symbol_token="1"
        )

    assert throttled_broker.calls == ["1"]
    assert aborted.status == "ABORTED_RATE_LIMIT"
    assert current == previous


def test_rejected_response_is_not_retried() -> None:
    instrument = _instrument("2", "Z-EQ")
    benchmark = BrokerInstrument(
        Exchange.NSE_CM,
        "1",
        "A INDEX",
        Decimal("0"),
        instrument_type="AMXIDX",
    )
    broker = _BatchBroker(
        {
            "1": {"status": False, "message": "invalid request", "data": None},
            "2": _historical_response(),
        }
    )
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        result = _service(broker, recorder).bootstrap(
            (instrument,), benchmark=benchmark, now=NOW
        )

    assert broker.calls == ["1", "2"]
    assert result.status == "COMPLETED_WITH_FAILURES"
    assert result.failed_symbols == 1
    assert result.symbols[0].reason_category == "BROKER_RESPONSE_ERROR"


def test_single_session_diagnostic_makes_exactly_one_request() -> None:
    broker = _BatchBroker({"1": _historical_response()})
    result = run_historical_diagnostic(
        broker,
        instrument=_instrument("1", "SBIN-EQ"),
        session_date=SESSION_DATE,
    )

    assert broker.calls == ["1"]
    assert result.status == "SUCCESS"
    assert result.row_count == 375
    assert result.first_timestamp == "2026-08-07T09:15:00+05:30"
    assert result.last_timestamp == "2026-08-07T15:29:00+05:30"
    assert result.to_dict()["order_placement_available"] is False


def test_throttled_diagnostic_makes_one_request_and_returns_non_success() -> None:
    broker = _BatchBroker({"1": _throttle()})
    result = run_historical_diagnostic(
        broker,
        instrument=_instrument("1", "SBIN-EQ"),
        session_date=SESSION_DATE,
    )

    assert broker.calls == ["1"]
    assert result.status == "ABORTED_RATE_LIMIT"
    assert result.successful is False
    assert result.reason_category == "RATE_LIMITED"


def test_30_day_history_is_split_at_session_boundaries_and_combined() -> None:
    instrument = _instrument("1", "A-EQ")

    class ChunkBroker:
        def __init__(self) -> None:
            self.parameters: list[dict[str, object]] = []

        def fetch_historical_candles(self, parameters):
            self.parameters.append(dict(parameters))
            start = date.fromisoformat(str(parameters["fromdate"])[:10])
            end = date.fromisoformat(str(parameters["todate"])[:10])
            if start <= SESSION_DATE <= end:
                return _historical_response()
            return {"status": True, "message": "SUCCESS", "data": []}

    broker = ChunkBroker()
    with SQLiteMarketRecorder("sqlite:///:memory:") as recorder:
        result = HistoricalBootstrapService(
            broker,
            recorder,
            lookback_days=30,
            minimum_sessions=1,
            request_delay_seconds=0,
            max_attempts=1,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
            sleeper=lambda _delay: None,
        ).bootstrap((instrument,), benchmark=_benchmark(), now=NOW)

    assert result.status == "COMPLETED"
    assert len(broker.parameters) == 4
    assert broker.parameters[0]["fromdate"] == "2026-07-11 09:15"
    assert broker.parameters[0]["todate"] == "2026-08-01 15:30"
    assert broker.parameters[1]["fromdate"] == "2026-08-02 09:15"
    assert broker.parameters[1]["todate"] == "2026-08-09 15:30"
