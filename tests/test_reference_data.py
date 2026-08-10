from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.broker import (
    AngelInstrumentMasterCache,
    AngelIntradayAllowedCache,
    BrokerInstrument,
    InstrumentMasterError,
    IntradayUniverseError,
)
from trading_bot.domain import IST, Exchange
from trading_bot.reference_data import Nifty500Cache, Nifty500CacheError
from trading_bot.reference_data.http import ReferenceDataDownloadError, download_https
from trading_bot.reference_data.nifty500 import (
    NIFTY500_CONSTITUENTS_URL,
    NIFTY500_OFFICIAL_FALLBACK_URL,
)
from trading_bot.reference_data.universe import resolve_nse_equity_universe

NOW = datetime(2026, 8, 10, 8, 30, tzinfo=IST)


def _instrument_bytes(*rows: dict[str, object]) -> bytes:
    return json.dumps(rows).encode()


def _instrument_row(
    token: str,
    symbol: str,
    *,
    exchange: str = "NSE",
    tick_size: str = "5.000000",
    lot_size: str = "1",
    instrument_type: str = "",
) -> dict[str, object]:
    return {
        "token": token,
        "symbol": symbol,
        "tick_size": tick_size,
        "lotsize": lot_size,
        "instrumenttype": instrument_type,
        "exch_seg": exchange,
    }


def _nifty_csv(*symbols: str) -> bytes:
    rows = ["Company Name,Industry,Symbol,Series,ISIN Code"]
    rows.extend(
        f"{symbol} Limited,Industry,{symbol},EQ,INE{index:07d}"
        for index, symbol in enumerate(symbols, start=1)
    )
    return ("\n".join(rows) + "\n").encode()


def test_instrument_refresh_is_atomic_and_validates_entire_source(tmp_path) -> None:
    path = tmp_path / "instruments.json"
    cache = AngelInstrumentMasterCache(path, max_age_seconds=3600)
    count = cache.refresh(
        source_bytes=_instrument_bytes(
            _instrument_row("3045", "SBIN-EQ"),
            _instrument_row(
                "99926000", "Nifty 50", instrument_type="AMXIDX", tick_size="0"
            ),
            # Valid official non-tradable sentinel rows are parsed but not cached.
            _instrument_row(
                "AGRIDEX",
                "AGRIDEX",
                exchange="NCDEX",
                tick_size="0",
                lot_size="-1",
                instrument_type="COMDTY",
            ),
        ),
        fetched_at=NOW,
    )

    assert count == 2
    assert cache.metadata.source_record_count == 3
    assert cache.find_trading_symbol(Exchange.NSE_CM, "SBIN-EQ") == BrokerInstrument(
        Exchange.NSE_CM,
        "3045",
        "SBIN-EQ",
        Decimal("0.05"),
    )
    last_known_good = path.read_bytes()

    malformed = _instrument_row("999", "BROKEN-EQ")
    del malformed["tick_size"]
    with pytest.raises(InstrumentMasterError, match="malformed"):
        cache.refresh(
            source_bytes=_instrument_bytes(malformed),
            fetched_at=NOW + timedelta(minutes=1),
        )

    assert path.read_bytes() == last_known_good
    fresh = AngelInstrumentMasterCache(path, max_age_seconds=3600).load(
        now=NOW + timedelta(minutes=2)
    )
    assert len(fresh.normal_nse_equities()) == 1


def test_duplicate_instrument_identity_and_corrupt_cache_fail_closed(tmp_path) -> None:
    path = tmp_path / "instruments.json"
    cache = AngelInstrumentMasterCache(path, max_age_seconds=3600)
    with pytest.raises(InstrumentMasterError, match="duplicate authoritative"):
        cache.refresh(
            source_bytes=_instrument_bytes(
                _instrument_row("1", "ONE-EQ"),
                _instrument_row("1", "TWO-EQ"),
            ),
            fetched_at=NOW,
        )

    path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(InstrumentMasterError, match="corrupt"):
        cache.load(now=NOW)


def test_intraday_snapshot_requires_same_date_and_freshness(tmp_path) -> None:
    cache = AngelIntradayAllowedCache(
        tmp_path / "intraday.json", max_age_seconds=8 * 3600
    )
    cache.refresh_from_response(
        {
            "status": True,
            "data": [
                {"Exchange": "NSE", "SymbolName": "SBIN", "Multiplier": "5"},
                {
                    "Exchange": "NSE",
                    "SymbolName": "RELIANCE",
                    "Multiplier": "4",
                },
            ],
        },
        fetched_at=NOW,
    )
    instrument = BrokerInstrument(Exchange.NSE_CM, "3045", "SBIN-EQ", Decimal("0.05"))

    cache.load(now=NOW + timedelta(hours=7))
    assert cache.is_intraday_allowed(instrument)
    assert cache.multiplier_for(instrument) == Decimal("5")

    with pytest.raises(IntradayUniverseError, match="stale"):
        AngelIntradayAllowedCache(cache.cache_path, max_age_seconds=8 * 3600).load(
            now=NOW + timedelta(days=1)
        )

    old_bytes = cache.cache_path.read_bytes()
    with pytest.raises(IntradayUniverseError, match="invalid multiplier"):
        cache.refresh_from_response(
            {"status": True, "data": [{"Exchange": "NSE", "SymbolName": "SBIN"}]},
            fetched_at=NOW,
        )
    assert cache.cache_path.read_bytes() == old_bytes


def test_nifty_cache_atomic_mapping_and_diagnostics(tmp_path) -> None:
    nifty_path = tmp_path / "nifty.json"
    nifty = Nifty500Cache(nifty_path, max_age_seconds=3600)
    nifty.refresh(
        source="fixture.csv", source_bytes=_nifty_csv("SBIN", "MISSING"), fetched_at=NOW
    )
    original = nifty_path.read_bytes()
    with pytest.raises(Nifty500CacheError, match="schema"):
        nifty.refresh(
            source="bad.csv",
            source_bytes=b"Symbol\nSBIN\n",
            fetched_at=NOW,
        )
    assert nifty_path.read_bytes() == original

    instruments = AngelInstrumentMasterCache(
        tmp_path / "instruments.json", max_age_seconds=3600
    )
    instruments.refresh(
        source_bytes=_instrument_bytes(_instrument_row("3045", "SBIN-EQ")),
        fetched_at=NOW,
    )
    intraday = AngelIntradayAllowedCache(
        tmp_path / "intraday.json", max_age_seconds=3600
    )
    intraday.refresh_from_response(
        {
            "status": True,
            "data": [{"Exchange": "NSE", "SymbolName": "SBIN", "Multiplier": "5"}],
        },
        fetched_at=NOW,
    )

    resolved = resolve_nse_equity_universe(
        nifty,
        instruments,
        intraday,
        configured_symbols=("SBIN", "MISSING", "NOTINDEX"),
    )

    assert [item.trading_symbol for item in resolved.instruments] == ["SBIN-EQ"]
    assert {(item.symbol, item.reason) for item in resolved.exclusions} == {
        ("MISSING", "missing_angel_mapping"),
        ("NOTINDEX", "not_in_current_nifty500_cache"),
    }


def test_ambiguous_nifty_mapping_is_excluded_without_guessing(tmp_path) -> None:
    nifty = Nifty500Cache(tmp_path / "nifty.json", max_age_seconds=3600)
    nifty.refresh(source="fixture.csv", source_bytes=_nifty_csv("ABC"), fetched_at=NOW)
    first = BrokerInstrument(Exchange.NSE_CM, "1", "ABC-EQ", Decimal("0.05"))
    second = BrokerInstrument(Exchange.NSE_CM, "2", "ABC-EQ", Decimal("0.05"))

    class AmbiguousMaster:
        def normal_nse_equities(self):
            return (first, second)

    class Allowed:
        def is_intraday_allowed(self, _instrument):
            return True

    resolved = resolve_nse_equity_universe(nifty, AmbiguousMaster(), Allowed())

    assert not resolved.instruments
    assert resolved.exclusions[0].reason == "ambiguous_angel_mapping"


def test_reference_downloader_has_bounded_retries(monkeypatch) -> None:
    attempts = 0
    delays: list[float] = []

    class FailingConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            nonlocal attempts
            attempts += 1

        def request(self, *_args, **_kwargs) -> None:
            raise OSError("offline")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "trading_bot.reference_data.http.http.client.HTTPSConnection",
        FailingConnection,
    )

    with pytest.raises(ReferenceDataDownloadError, match="3 attempts"):
        download_https(
            "https://example.test/data.json",
            connect_timeout_seconds=1,
            read_timeout_seconds=2,
            max_attempts=3,
            max_bytes=100,
            sleeper=delays.append,
        )

    assert attempts == 3
    assert delays == [1, 2]


def test_nifty_refresh_uses_only_official_https_hosts_and_official_fallback(
    tmp_path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_download(url: str, **_kwargs) -> bytes:
        calls.append(url)
        if url == NIFTY500_CONSTITUENTS_URL:
            raise ReferenceDataDownloadError("primary unavailable")
        return _nifty_csv("SBIN")

    monkeypatch.setattr(
        "trading_bot.reference_data.nifty500.download_https", fake_download
    )
    cache = Nifty500Cache(tmp_path / "nifty.json", max_age_seconds=3600)

    assert cache.refresh(fetched_at=NOW) == 1
    assert calls == [NIFTY500_CONSTITUENTS_URL, NIFTY500_OFFICIAL_FALLBACK_URL]
    assert cache.metadata.source_url == NIFTY500_OFFICIAL_FALLBACK_URL

    with pytest.raises(Nifty500CacheError, match="official NSE host"):
        cache.refresh(source="https://unofficial.example/nifty500.csv", fetched_at=NOW)
