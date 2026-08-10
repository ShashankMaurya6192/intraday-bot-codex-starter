from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date, datetime

from .broker import (
    AngelCredentials,
    AngelInstrumentMasterCache,
    AngelIntradayAllowedCache,
    AngelOneMarketDataAdapter,
    InstrumentMasterError,
    IntradayUniverseError,
    ReconnectPolicy,
)
from .config import Settings
from .domain import IST, Exchange
from .marketdata import MarketReplayEngine
from .reference_data import (
    NIFTY500_CONSTITUENTS_URL,
    Nifty500Cache,
    Nifty500CacheError,
)
from .reference_data.universe import resolve_nse_equity_universe
from .scanner import MarketScanner, ScannerWeights
from .storage import SQLiteMarketRecorder, TickIngestionService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot",
        description="Paper-first Angel One market intelligence (analysis only)",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="show redacted Phase 2 configuration status")
    subparsers.add_parser("init-db", help="create or migrate the SQLite recorder")
    subparsers.add_parser(
        "record-market",
        help="authenticate and record Angel One WebSocket market data (no orders)",
    )
    subparsers.add_parser(
        "refresh-instruments",
        help="explicitly refresh the official Angel instrument-master cache",
    )
    subparsers.add_parser(
        "refresh-intraday-universe",
        help="explicitly refresh today's authenticated Angel NSE intraday snapshot",
    )
    nifty = subparsers.add_parser(
        "refresh-nifty500",
        help="explicitly refresh/import the NSE Indices NIFTY 500 CSV",
    )
    nifty.add_argument("--source", default=NIFTY500_CONSTITUENTS_URL)
    data_status = subparsers.add_parser(
        "data-status", help="show cache age/count/hash and local market-data status"
    )
    data_status.add_argument("--json", action="store_true", dest="json_output")
    replay = subparsers.add_parser(
        "replay", help="deterministically replay a recorded IST session"
    )
    replay.add_argument("--date", required=True, type=_iso_date)
    replay.add_argument("--json", action="store_true", dest="json_output")
    scan = subparsers.add_parser(
        "scan", help="rank analysis-only LONG/SHORT attention candidates"
    )
    scan.add_argument("--json", action="store_true", dest="json_output")
    scan.add_argument("--limit", type=int, default=10)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings()

    # Phase 2 intentionally has no explicit-live confirmation path. Any LIVE
    # configuration therefore fails closed before a command runs.
    settings.assert_live_allowed()
    command = args.command or "status"

    if command == "status":
        subscriptions = settings.market_subscriptions()
        print(f"Intraday Bot | mode={settings.trading_mode.value} | phase=2")
        print(f"Angel credentials configured={settings.angel_credentials_configured}")
        print(f"Market subscriptions={len(subscriptions)}")
        print(f"Scanner symbol override={len(settings.configured_scanner_symbols())}")
        print(f"Database={settings.database_url}")
        print("Scanner output=ANALYSIS_ONLY")
        print("Paper execution available=False")
        print("Order placement available=False")
        return 0

    if command == "init-db":
        with _recorder(settings):
            pass
        print(f"Initialized SQLite market recorder at {settings.database_url}")
        return 0

    if command == "record-market":
        return _record_market(settings)

    if command == "refresh-instruments":
        cache = _instrument_cache(settings)
        count = cache.refresh()
        print(
            f"Refreshed Angel instrument master: records={count} "
            f"sha256={cache.metadata.sha256} cache={cache.cache_path}"
        )
        return 0

    if command == "refresh-intraday-universe":
        _require_credentials(settings)
        adapter = AngelOneMarketDataAdapter(_credentials(settings))
        response = adapter.fetch_nse_intraday()
        cache = _intraday_cache(settings)
        count = cache.refresh_from_response(response)
        print(
            f"Refreshed current Angel NSE intraday universe: records={count} "
            f"sha256={cache.metadata.sha256} cache={cache.cache_path}"
        )
        return 0

    if command == "refresh-nifty500":
        cache = _nifty500_cache(settings)
        count = cache.refresh(source=args.source)
        print(
            f"Refreshed NIFTY 500 constituents: records={count} "
            f"sha256={cache.metadata.sha256} cache={cache.cache_path}"
        )
        return 0

    if command == "data-status":
        return _data_status(settings, json_output=args.json_output)

    if command == "replay":
        return _replay(settings, args.date, json_output=args.json_output)

    if command == "scan":
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        return _scan(settings, json_output=args.json_output, limit=args.limit)

    raise AssertionError(f"unhandled command: {command}")


def _record_market(settings: Settings) -> int:
    _require_credentials(settings)
    subscriptions = settings.market_subscriptions()
    if not subscriptions:
        raise RuntimeError(
            "ANGEL_MARKET_SUBSCRIPTIONS is empty; no market data can be recorded"
        )
    credentials = _credentials(settings)
    policy = ReconnectPolicy(
        max_attempts=settings.websocket_max_reconnect_attempts,
        initial_delay_seconds=settings.websocket_reconnect_initial_delay_seconds,
        multiplier=settings.websocket_reconnect_multiplier,
        max_delay_seconds=settings.websocket_reconnect_max_delay_seconds,
        stable_connection_seconds=settings.websocket_stable_connection_seconds,
    )
    adapter = AngelOneMarketDataAdapter(
        credentials,
        subscription_mode=settings.angel_subscription_mode,
        correlation_id=settings.angel_correlation_id,
        reconnect_policy=policy,
    )
    with _recorder(settings) as recorder:
        ingestion = TickIngestionService(
            recorder,
            queue_capacity=settings.tick_ingestion_queue_capacity,
            batch_size=settings.tick_ingestion_batch_size,
            flush_interval_seconds=settings.tick_ingestion_flush_interval_seconds,
            shutdown_timeout_seconds=settings.tick_ingestion_shutdown_timeout_seconds,
            on_unhealthy=lambda _error: adapter.disconnect(),
        )
        ingestion.start()
        print(
            f"Recording {len(subscriptions)} subscription(s) in PAPER mode. "
            "Press Ctrl+C to stop."
        )
        try:
            adapter.stream(
                subscriptions,
                ingestion.submit,
                recorder.record_feed_event,
            )
        except KeyboardInterrupt:
            print("Stopping market recorder...")
        finally:
            adapter.disconnect()
            ingestion.stop(raise_on_failure=False)
        if ingestion.failure is not None:
            raise ingestion.failure
    return 0


def _data_status(settings: Settings, *, json_output: bool) -> int:
    now = datetime.now(IST)
    rows = [
        _cache_status("instrument_master", _instrument_cache(settings), now),
        _cache_status("intraday_universe", _intraday_cache(settings), now),
        _cache_status("nifty500", _nifty500_cache(settings), now),
    ]
    result = {
        "checked_at": now.isoformat(),
        "caches": rows,
        "order_placement_available": False,
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(
                f"{row['name']}: {row['status']} age_seconds={row['age_seconds']} "
                f"records={row['record_count']} sha256={row['sha256']}"
            )
            if row["reason"]:
                print(f"  reason={row['reason']}")
    return 0 if all(row["status"] == "READY" for row in rows) else 2


def _replay(settings: Settings, session_date: date, *, json_output: bool) -> int:
    engine = _replay_engine(settings)
    with _recorder(settings) as recorder:
        result = engine.replay_date(recorder, session_date, persist=True)
    payload = {
        "date": session_date.isoformat(),
        "accepted_ticks": result.accepted_ticks,
        "rejected_ticks": result.rejected_ticks,
        "completed_candles": len(result.candles),
        "trusted_candles": sum(candle.trusted for candle in result.candles),
        "instruments": len(result.health),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "Replay complete: "
            + " ".join(f"{key}={value}" for key, value in payload.items())
        )
    return 0


def _scan(settings: Settings, *, json_output: bool, limit: int) -> int:
    now = datetime.now(IST)
    try:
        instrument_cache = _instrument_cache(settings).load(now=now)
        intraday_cache = _intraday_cache(settings).load(now=now)
        nifty_cache = _nifty500_cache(settings).load(now=now)
        universe = resolve_nse_equity_universe(
            nifty_cache,
            instrument_cache,
            intraday_cache,
            configured_symbols=settings.configured_scanner_symbols(),
        )
        benchmark = instrument_cache.find_trading_symbol(
            Exchange.NSE_CM, settings.nifty_benchmark_trading_symbol
        )
    except (InstrumentMasterError, IntradayUniverseError, Nifty500CacheError) as exc:
        payload = {
            "readiness": "NOT_READY",
            "market_regime": "UNKNOWN",
            "diagnostics": [type(exc).__name__],
            "analysis_only": True,
            "long_candidates": [],
            "short_candidates": [],
        }
        _print_scan_payload(payload, json_output=json_output, limit=limit)
        return 2

    with _recorder(settings) as recorder:
        replay = _replay_engine(settings).replay_date(
            recorder,
            now.date(),
            evaluation_at=now,
            persist=False,
        )
        candles_by: dict[tuple[Exchange, str], list] = {}
        for candle in replay.candles:
            candles_by.setdefault((candle.exchange, candle.symbol_token), []).append(
                candle
            )
        health_by = {
            (health.exchange, health.symbol_token): health for health in replay.health
        }
        scanner = MarketScanner(
            weights=ScannerWeights(
                vwap=settings.scanner_weight_vwap,
                ema=settings.scanner_weight_ema,
                relative_volume=settings.scanner_weight_relative_volume,
                relative_strength=settings.scanner_weight_relative_strength,
                opening_range_proximity=settings.scanner_weight_opening_range,
                market_regime=settings.scanner_weight_market_regime,
            ),
            relative_volume_threshold=settings.relative_volume_threshold,
            relative_volume_lookback_sessions=(
                settings.relative_volume_lookback_sessions
            ),
            relative_volume_minimum_sessions=(
                settings.relative_volume_minimum_sessions
            ),
            opening_range_minutes=settings.opening_range_minutes,
            breakout_proximity_pct=settings.scanner_breakout_proximity_pct,
            regime_momentum_threshold_pct=(settings.regime_momentum_threshold_pct),
            regime_slope_lookback_candles=(settings.regime_slope_lookback_candles),
        )
        result = scanner.scan(
            instruments=universe.instruments,
            candles_by_instrument=candles_by,
            health_by_instrument=health_by,
            benchmark=benchmark,
            baseline_store=recorder,
            now=now,
            universe_diagnostics=[
                f"{item.symbol}:{item.reason}" for item in universe.exclusions
            ],
        )
    payload = result.to_dict()
    _print_scan_payload(payload, json_output=json_output, limit=limit)
    return 0 if result.readiness.value == "READY" else 2


def _print_scan_payload(
    payload: dict[str, object], *, json_output: bool, limit: int
) -> None:
    if json_output:
        limited = dict(payload)
        limited["long_candidates"] = list(payload.get("long_candidates", []))[:limit]
        limited["short_candidates"] = list(payload.get("short_candidates", []))[:limit]
        print(json.dumps(limited, indent=2, sort_keys=True))
        return
    print(
        f"Scanner readiness={payload['readiness']} "
        f"regime={payload['market_regime']} analysis_only=True"
    )
    print("BIAS  SYMBOL              SCORE      LTP     RVOL       RS")
    for bias_key in ("long_candidates", "short_candidates"):
        for item in list(payload.get(bias_key, []))[:limit]:
            print(
                f"{item['bias']:<5} {item['symbol']:<19} "
                f"{item['total_score']:>5} {item['ltp']:>8} "
                f"{item['relative_volume']:>8} {item['relative_strength']:>8}"
            )
    diagnostics = list(payload.get("diagnostics", []))
    print(f"diagnostics={len(diagnostics)}")
    for reason in diagnostics[:10]:
        print(f"  {reason}")


def _cache_status(name: str, cache, now: datetime) -> dict[str, object]:
    try:
        cache.load(now=now, require_current=False)
        metadata = cache.metadata
        age = max(0.0, (now - metadata.fetched_at).total_seconds())
        try:
            cache.load(now=now, require_current=True)
            status = "READY"
            reason = None
        except Exception as exc:
            status = "NOT_READY"
            reason = type(exc).__name__
        return {
            "name": name,
            "status": status,
            "age_seconds": round(age, 3),
            "record_count": metadata.source_record_count or metadata.record_count,
            "sha256": metadata.sha256,
            "source_url": metadata.source_url,
            "reason": reason,
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "NOT_READY",
            "age_seconds": None,
            "record_count": None,
            "sha256": None,
            "source_url": None,
            "reason": type(exc).__name__,
        }


def _instrument_cache(settings: Settings) -> AngelInstrumentMasterCache:
    return AngelInstrumentMasterCache(
        settings.instrument_master_cache_path,
        max_age_seconds=settings.instrument_master_max_age_days * 86_400,
        connect_timeout_seconds=settings.reference_download_connect_timeout_seconds,
        read_timeout_seconds=settings.reference_download_read_timeout_seconds,
        refresh_attempts=settings.reference_download_max_attempts,
    )


def _intraday_cache(settings: Settings) -> AngelIntradayAllowedCache:
    return AngelIntradayAllowedCache(
        settings.intraday_universe_cache_path,
        max_age_seconds=settings.intraday_universe_max_age_seconds,
    )


def _nifty500_cache(settings: Settings) -> Nifty500Cache:
    return Nifty500Cache(
        settings.nifty500_cache_path,
        max_age_seconds=settings.nifty500_max_age_days * 86_400,
        connect_timeout_seconds=settings.reference_download_connect_timeout_seconds,
        read_timeout_seconds=settings.reference_download_read_timeout_seconds,
        refresh_attempts=settings.reference_download_max_attempts,
    )


def _replay_engine(settings: Settings) -> MarketReplayEngine:
    return MarketReplayEngine(
        stale_after_seconds=settings.market_data_stale_after_seconds,
        recovery_consecutive_ticks=settings.market_data_recovery_consecutive_ticks,
        allowed_sequence_gap=settings.market_data_allowed_sequence_gap,
    )


def _recorder(settings: Settings) -> SQLiteMarketRecorder:
    return SQLiteMarketRecorder(
        settings.database_url,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    )


def _credentials(settings: Settings) -> AngelCredentials:
    return AngelCredentials(
        api_key=settings.angel_api_key.get_secret_value(),
        client_code=settings.angel_client_code.get_secret_value(),
        password=settings.angel_password.get_secret_value(),
        totp_secret=settings.angel_totp_secret.get_secret_value(),
    )


def _require_credentials(settings: Settings) -> None:
    if not settings.angel_credentials_configured:
        raise RuntimeError(
            "Angel One credentials are incomplete; populate the local .env file"
        )


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


if __name__ == "__main__":
    raise SystemExit(main())
