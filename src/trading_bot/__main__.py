from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from time import perf_counter

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
from .domain import IST, Exchange, HistoricalProvider, MarketSubscription
from .marketdata import (
    Candle,
    HistoricalBootstrapService,
    MarketIntelligenceProjection,
    MarketReplayEngine,
    ProjectionCutoffBehindCheckpointError,
    ProjectionError,
    ReplaySafetyLimitExceeded,
    YFinanceHistoricalProvider,
    run_historical_diagnostic,
)
from .reference_data import (
    NIFTY500_CONSTITUENTS_URL,
    Nifty500Cache,
    Nifty500CacheError,
)
from .reference_data.universe import (
    ScannerSubscriptionError,
    build_scanner_subscription_plan,
    resolve_nse_equity_mappings,
    resolve_nse_equity_universe,
)
from .scanner import MarketScanner, ScannerWeights
from .storage import (
    ProjectionMarketDataWriter,
    SQLiteMarketRecorder,
    TickIngestionService,
)


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
        "record-scanner-market",
        help="record the validated local scanner universe plus NIFTY (no orders)",
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
    history = subparsers.add_parser(
        "bootstrap-history",
        help="explicitly bootstrap prior 1m/5m research history",
    )
    history.add_argument("--force", action="store_true")
    history.add_argument("--json", action="store_true", dest="json_output")
    diagnostic = subparsers.add_parser(
        "test-historical",
        help="make exactly one SmartAPI historical request for one prior session",
    )
    diagnostic.add_argument("--symbol", required=True)
    diagnostic.add_argument("--date", required=True, type=_iso_date)
    diagnostic.add_argument("--json", action="store_true", dest="json_output")
    provider_diagnostic = subparsers.add_parser(
        "test-history-provider",
        help="validate a PAPER-only historical provider without writing data",
    )
    provider_diagnostic.add_argument(
        "--provider",
        required=True,
        choices=[HistoricalProvider.YFINANCE.value],
    )
    provider_diagnostic.add_argument("--symbol", required=True)
    provider_diagnostic.add_argument("--lookback-days", type=int, default=30)
    provider_diagnostic.add_argument("--json", action="store_true", dest="json_output")
    scan = subparsers.add_parser(
        "scan", help="rank analysis-only LONG/SHORT attention candidates"
    )
    scan.add_argument("--json", action="store_true", dest="json_output")
    scan.add_argument("--limit", type=int, default=10)
    compare = subparsers.add_parser(
        "compare-projection-replay",
        help="compare materialized scanner state with deterministic raw replay",
    )
    compare.add_argument("--date", required=True, type=_iso_date)
    compare.add_argument("--json", action="store_true", dest="json_output")
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
        print(f"Intraday Bot | mode={settings.trading_mode.value} | phase=2.5.3")
        print(f"Angel credentials configured={settings.angel_credentials_configured}")
        print(f"Market subscriptions={len(subscriptions)}")
        print(f"Scanner symbol override={len(settings.configured_scanner_symbols())}")
        print(f"Database={settings.database_url}")
        print(f"Historical provider={settings.historical_provider.value}")
        print(f"Historical fallback enabled={settings.historical_fallback_enabled}")
        print(f"Maximum VWAP distance ATR={settings.max_vwap_distance_atr}")
        print(
            "Scanner projection stale after seconds="
            f"{settings.scanner_projection_stale_after_seconds}"
        )
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

    if command == "record-scanner-market":
        return _record_scanner_market(settings)

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

    if command == "bootstrap-history":
        return _bootstrap_history(
            settings,
            force=args.force,
            json_output=args.json_output,
        )

    if command == "test-historical":
        return _test_historical(
            settings,
            symbol=args.symbol,
            session_date=args.date,
            json_output=args.json_output,
        )

    if command == "test-history-provider":
        return _test_history_provider(
            settings,
            provider=HistoricalProvider(args.provider),
            symbol=args.symbol,
            lookback_days=args.lookback_days,
            json_output=args.json_output,
        )

    if command == "scan":
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        return _scan(settings, json_output=args.json_output, limit=args.limit)

    if command == "compare-projection-replay":
        return _compare_projection_replay(
            settings,
            session_date=args.date,
            json_output=args.json_output,
        )

    raise AssertionError(f"unhandled command: {command}")


def _record_market(settings: Settings) -> int:
    _require_credentials(settings)
    subscriptions = settings.market_subscriptions()
    if not subscriptions:
        raise RuntimeError(
            "ANGEL_MARKET_SUBSCRIPTIONS is empty; no market data can be recorded"
        )
    return _record_subscriptions(
        settings,
        subscriptions,
        description=f"{len(subscriptions)} diagnostic subscription(s)",
    )


def _record_scanner_market(settings: Settings) -> int:
    now = datetime.now(IST)
    instrument_cache = _instrument_cache(settings).load(now=now)
    intraday_cache = _intraday_cache(settings).load(now=now)
    nifty_cache = _nifty500_cache(settings).load(now=now)
    plan = build_scanner_subscription_plan(
        nifty_cache,
        instrument_cache,
        intraday_cache,
        benchmark_trading_symbol=settings.nifty_benchmark_trading_symbol,
        configured_symbols=settings.configured_scanner_symbols(),
    )
    print(
        "Scanner subscriptions: "
        f"requested={plan.requested_count} eligible={plan.eligible_count} "
        f"expected_ineligible={plan.expected_ineligible_count} "
        f"mapping_failures={plan.mapping_failure_count} "
        f"benchmark={plan.benchmark_count} final={len(plan.subscriptions)}"
    )
    _require_credentials(settings)
    return _record_subscriptions(
        settings,
        plan.subscriptions,
        description=f"{len(plan.subscriptions)} scanner subscription(s)",
        incremental_projection=True,
    )


def _record_subscriptions(
    settings: Settings,
    subscriptions: Sequence[MarketSubscription],
    *,
    description: str,
    incremental_projection: bool = False,
) -> int:
    if len(subscriptions) > 1_000:
        raise ScannerSubscriptionError(
            "market-data subscriptions exceed the 1000-token SmartAPI limit"
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
        projection = None
        writer = recorder
        if incremental_projection:
            projection = _projection(settings, recorder, datetime.now(IST).date())
            update = projection.initialize(
                now=datetime.now(IST),
                allow_rebuild=True,
            )
            writer = ProjectionMarketDataWriter(recorder, projection)
            print(
                "Scanner projection: "
                f"rebuilt={update.rebuilt} "
                f"incremental_ticks_caught_up={update.incremental_ticks_processed} "
                f"feed_events_caught_up={update.incremental_feed_events_processed}"
            )
        ingestion = TickIngestionService(
            writer,
            queue_capacity=settings.tick_ingestion_queue_capacity,
            batch_size=settings.tick_ingestion_batch_size,
            flush_interval_seconds=settings.tick_ingestion_flush_interval_seconds,
            shutdown_timeout_seconds=settings.tick_ingestion_shutdown_timeout_seconds,
            on_unhealthy=lambda _error: adapter.disconnect(),
        )
        ingestion.start()
        print(
            f"Recording {description} in PAPER mode. "
            "Press Ctrl+C to stop."
        )
        try:
            adapter.stream(
                subscriptions,
                ingestion.submit,
                (
                    ingestion.submit_feed_event
                    if incremental_projection
                    else recorder.record_feed_event
                ),
            )
        except KeyboardInterrupt:
            print("Stopping market recorder...")
        finally:
            adapter.disconnect()
            ingestion.stop(raise_on_failure=False)
            if projection is not None and ingestion.failure is None:
                projection.view(now=datetime.now(IST))
        if ingestion.failure is not None:
            raise ingestion.failure
    return 0


def _bootstrap_history(
    settings: Settings, *, force: bool, json_output: bool
) -> int:
    now = datetime.now(IST)
    instrument_cache = _instrument_cache(settings).load(now=now)
    nifty_cache = _nifty500_cache(settings).load(now=now)
    mapped = resolve_nse_equity_mappings(
        nifty_cache,
        instrument_cache,
        configured_symbols=settings.configured_scanner_symbols(),
    )
    if mapped.mapping_failures:
        failures = ",".join(
            f"{item.symbol}:{item.reason}" for item in mapped.mapping_failures
        )
        raise RuntimeError(f"historical symbol mapping failed closed: {failures}")
    benchmark = instrument_cache.find_trading_symbol(
        Exchange.NSE_CM, settings.nifty_benchmark_trading_symbol
    )
    if benchmark is None or benchmark.instrument_type != "AMXIDX":
        raise RuntimeError("authoritative NIFTY benchmark mapping is unavailable")
    adapter = None
    if settings.historical_provider in {
        HistoricalProvider.ANGEL,
        HistoricalProvider.AUTO,
    }:
        _require_credentials(settings)
        adapter = AngelOneMarketDataAdapter(_credentials(settings))
    fallback = (
        YFinanceHistoricalProvider()
        if settings.historical_fallback_enabled
        and settings.historical_provider
        in {HistoricalProvider.YFINANCE, HistoricalProvider.AUTO}
        else None
    )
    with _recorder(settings) as recorder:
        service = HistoricalBootstrapService(
            adapter,
            recorder,
            lookback_days=settings.historical_bootstrap_lookback_days,
            minimum_sessions=settings.historical_bootstrap_minimum_sessions,
            request_delay_seconds=settings.historical_request_delay_seconds,
            max_attempts=settings.historical_request_max_attempts,
            retry_initial_delay_seconds=(
                settings.historical_retry_initial_delay_seconds
            ),
            retry_max_delay_seconds=settings.historical_retry_max_delay_seconds,
            provider=settings.historical_provider,
            fallback_enabled=settings.historical_fallback_enabled,
            fallback_provider=fallback,
            trading_mode=settings.trading_mode,
        )
        result = service.bootstrap(
            mapped.instruments,
            benchmark=benchmark,
            now=now,
            force=force,
        )
    payload = result.to_dict()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "Historical bootstrap: "
            f"status={result.status} "
            f"requested={result.requested_symbols} "
            f"completed={result.completed_symbols} failed={result.failed_symbols} "
            f"skipped_after_abort={result.skipped_after_abort} "
            f"rate_limited_symbol={result.rate_limited_symbol} "
            f"reason_category={result.reason_category} "
            f"sessions={result.sessions_found} "
            f"cache_age_seconds={result.cache_age_seconds} "
            "order_placement_available=False"
        )
        for item in result.symbols:
            print(
                f"  {item.symbol}: {item.status} sessions={item.sessions_found} "
                f"cache_age_seconds={item.cache_age_seconds} "
                f"reason_category={item.reason_category} reason={item.reason}"
                f" provider={item.provider} provenance={item.provenance}"
            )
        if result.operator_action is not None:
            print(f"Operator action: {result.operator_action}")
    return 0 if result.failed_symbols == 0 else 2


def _test_historical(
    settings: Settings,
    *,
    symbol: str,
    session_date: date,
    json_output: bool,
) -> int:
    now = datetime.now(IST)
    if session_date >= now.date():
        raise ValueError("--date must identify a completed prior session")
    instrument_cache = _instrument_cache(settings).load(now=now)
    trading_symbol = symbol.strip().upper()
    if not trading_symbol.endswith("-EQ"):
        trading_symbol = f"{trading_symbol}-EQ"
    instrument = instrument_cache.find_trading_symbol(
        Exchange.NSE_CM, trading_symbol
    )
    if instrument is None or not instrument.is_normal_nse_equity:
        raise RuntimeError(
            "diagnostic symbol is unavailable as an authoritative normal NSE equity"
        )
    _require_credentials(settings)
    result = run_historical_diagnostic(
        AngelOneMarketDataAdapter(_credentials(settings)),
        instrument=instrument,
        session_date=session_date,
    )
    payload = result.to_dict()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Historical diagnostic: status={result.status} "
            f"symbol={result.symbol} date={result.session_date.isoformat()} "
            f"broker_status={result.broker_status} "
            f"message={result.broker_message} errorcode={result.error_code} "
            f"rows={result.row_count} first={result.first_timestamp} "
            f"last={result.last_timestamp} "
            f"reason_category={result.reason_category} reason={result.reason} "
            "order_placement_available=False"
        )
    return 0 if result.successful else 2


def _test_history_provider(
    settings: Settings,
    *,
    provider: HistoricalProvider,
    symbol: str,
    lookback_days: int,
    json_output: bool,
) -> int:
    if not 1 <= lookback_days <= 30:
        raise ValueError("--lookback-days must be between 1 and 30")
    if provider is not HistoricalProvider.YFINANCE:
        raise ValueError("only the yfinance diagnostic is available in Phase 2.3")
    now = datetime.now(IST)
    instrument_cache = _instrument_cache(settings).load(now=now)
    requested = symbol.strip().upper()
    if requested in {"NIFTY", "NIFTY50", "NIFTY 50"}:
        trading_symbol = settings.nifty_benchmark_trading_symbol
    else:
        trading_symbol = requested if requested.endswith("-EQ") else f"{requested}-EQ"
    instrument = instrument_cache.find_trading_symbol(Exchange.NSE_CM, trading_symbol)
    if instrument is None or not (
        instrument.is_normal_nse_equity or instrument.instrument_type == "AMXIDX"
    ):
        raise RuntimeError(
            "diagnostic symbol is unavailable in the authoritative instrument cache"
        )
    through_date = now.date() - timedelta(days=1)
    from_date = through_date - timedelta(days=lookback_days - 1)
    result = YFinanceHistoricalProvider().diagnose(
        instrument=instrument,
        from_date=from_date,
        through_date=through_date,
        minimum_sessions=settings.historical_bootstrap_minimum_sessions,
    )
    payload = result.to_dict()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        discarded = ",".join(
            item.isoformat() for item in result.discarded_partial_sessions
        )
        print(
            "History provider diagnostic: "
            f"status={result.status} provider={provider.value} "
            f"symbol={result.symbol} provider_symbol={result.provider_symbol} "
            f"chunks={result.requested_chunks} rows={result.row_count} "
            f"complete_sessions={result.complete_sessions} "
            f"discarded_partial_sessions={discarded} "
            f"first={result.first_timestamp} last={result.last_timestamp} "
            f"validation={result.validation} reason={result.reason} "
            "order_placement_available=False"
        )
    return 0 if result.successful else 2


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
        "raw_ticks_replayed": result.raw_ticks_replayed,
        "ignored_future_ticks": result.ignored_future_ticks,
        "session_references": len(result.session_references),
        "opening_range_references": len(result.opening_range_references),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "Replay complete: "
            + " ".join(f"{key}={value}" for key, value in payload.items())
        )
    return 0


def _compare_projection_replay(
    settings: Settings, *, session_date: date, json_output: bool
) -> int:
    """Compare materialized and raw-replay semantics at one deterministic cutoff."""
    now = _ist_now()
    session_close = datetime.combine(session_date, time(15, 30), tzinfo=IST)
    evaluation_at = min(now, session_close)
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
        if benchmark is not None and benchmark.instrument_type != "AMXIDX":
            benchmark = None
        with _recorder(settings) as recorder:
            view = recorder.load_projection_view(session_date)
            snapshot_observed_at = _ist_now()
            if _projection_timestamp_is_future(
                now=snapshot_observed_at,
                last_received_at=view.checkpoint.last_received_at,
                checkpoint_updated_at=view.checkpoint.updated_at,
            ):
                raise ProjectionCutoffBehindCheckpointError(
                    "projection checkpoint is ahead of the latest read time"
                )
            evaluation_at = view.checkpoint.updated_at
            replay = _replay_engine(settings).replay_date(
                recorder,
                session_date,
                evaluation_at=evaluation_at,
                persist=False,
                hard_tick_limit=settings.scan_replay_hard_tick_limit,
            )
            projection_scan = _scanner_result_for_comparison(
                settings=settings,
                recorder=recorder,
                universe=universe,
                benchmark=benchmark,
                current_candles=view.candles,
                health=view.health,
                session_references=view.session_references,
                opening_references=view.opening_range_references,
                evaluation_at=evaluation_at,
            )
            replay_scan = _scanner_result_for_comparison(
                settings=settings,
                recorder=recorder,
                universe=universe,
                benchmark=benchmark,
                current_candles=replay.candles,
                health=replay.health,
                session_references=replay.session_references,
                opening_references=replay.opening_range_references,
                evaluation_at=evaluation_at,
            )
    except (
        InstrumentMasterError,
        IntradayUniverseError,
        Nifty500CacheError,
        ProjectionError,
        ReplaySafetyLimitExceeded,
    ) as exc:
        payload = {
            "date": session_date.isoformat(),
            "as_of": evaluation_at.isoformat(),
            "equivalent": False,
            "mismatches": [f"{type(exc).__name__}:{exc}"],
            "order_placement_available": False,
        }
        _print_comparison_payload(payload, json_output=json_output)
        return 2

    candle_difference = _candle_difference(view.candles, replay.candles)
    comparisons = {
        "health": _health_comparison(view.health)
        == _health_comparison(replay.health),
        "candles": not any(
            candle_difference[key]
            for key in (
                "only_projection_count",
                "only_replay_count",
                "changed_count",
            )
        ),
        "session_references": view.session_references
        == replay.session_references,
        "opening_range_references": view.opening_range_references
        == replay.opening_range_references,
        "scanner": _scanner_comparison(projection_scan)
        == _scanner_comparison(replay_scan),
    }
    mismatches = [name for name, matched in comparisons.items() if not matched]
    payload = {
        "date": session_date.isoformat(),
        "as_of": evaluation_at.isoformat(),
        "equivalent": not mismatches,
        "mismatches": mismatches[:10],
        "projection_last_tick_id": view.checkpoint.last_tick_id,
        "raw_ticks_replayed": replay.raw_ticks_replayed,
        "projection_readiness": projection_scan.readiness.value,
        "replay_readiness": replay_scan.readiness.value,
        "candle_difference": candle_difference,
        "order_placement_available": False,
    }
    _print_comparison_payload(payload, json_output=json_output)
    return 0 if not mismatches else 2


def _scanner_result_for_comparison(
    *,
    settings: Settings,
    recorder: SQLiteMarketRecorder,
    universe,
    benchmark,
    current_candles: Sequence[Candle],
    health: Sequence,
    session_references: Sequence,
    opening_references: Sequence,
    evaluation_at: datetime,
):
    historical_start = datetime.combine(
        evaluation_at.date()
        - timedelta(days=settings.historical_bootstrap_lookback_days),
        time.min,
        tzinfo=IST,
    )
    session_start = datetime.combine(evaluation_at.date(), time.min, tzinfo=IST)
    prior = recorder.load_candles(
        start_at=historical_start,
        end_at=session_start,
    )
    candles_by = _candles_by_instrument((*prior, *current_candles))
    health_by = {(item.exchange, item.symbol_token): item for item in health}
    return _market_scanner(settings).scan(
        instruments=universe.instruments,
        candles_by_instrument=candles_by,
        health_by_instrument=health_by,
        benchmark=benchmark,
        baseline_store=recorder,
        now=evaluation_at,
        expected_eligibility_diagnostics=[
            f"{item.symbol}:{item.reason}"
            for item in universe.expected_eligibility_exclusions
        ],
        reference_diagnostics=[
            f"{item.symbol}:{item.reason}" for item in universe.mapping_failures
        ],
        requested_count=len(settings.configured_scanner_symbols()),
        session_references={
            (item.exchange, item.symbol_token): item for item in session_references
        },
        opening_range_references={
            (item.exchange, item.symbol_token): item for item in opening_references
        },
    )


def _candles_by_instrument(
    candles: Sequence[Candle],
) -> dict[tuple[Exchange, str], list[Candle]]:
    grouped: dict[
        tuple[Exchange, str], dict[tuple[int, datetime], Candle]
    ] = {}
    for candle in candles:
        grouped.setdefault((candle.exchange, candle.symbol_token), {})[
            (int(candle.interval), candle.start_at)
        ] = candle
    return {
        key: sorted(values.values(), key=lambda item: (item.start_at, item.interval))
        for key, values in grouped.items()
    }


def _health_comparison(items: Sequence) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.exchange.value,
            item.symbol_token,
            item.feed_epoch,
            item.status.value,
            item.latest_exchange_timestamp,
            item.latest_received_timestamp,
            item.sequence_number,
            item.duplicate_count,
            item.out_of_order_count,
            item.sequence_jump_count,
        )
        for item in items
    )


def _candle_difference(
    projection: Sequence[Candle], replay: Sequence[Candle]
) -> dict[str, object]:
    """Return bounded, JSON-safe candle mismatch diagnostics."""
    projection_by = {_candle_identity(item): item for item in projection}
    replay_by = {_candle_identity(item): item for item in replay}
    projection_keys = set(projection_by)
    replay_keys = set(replay_by)
    only_projection = sorted(projection_keys - replay_keys)
    only_replay = sorted(replay_keys - projection_keys)
    changed = [
        key
        for key in sorted(projection_keys & replay_keys)
        if _candle_field_values(projection_by[key])
        != _candle_field_values(replay_by[key])
    ]
    example_limit = 5
    return {
        "projection_count": len(projection_by),
        "replay_count": len(replay_by),
        "only_projection_count": len(only_projection),
        "only_replay_count": len(only_replay),
        "changed_count": len(changed),
        "only_projection_examples": [
            _candle_identity_payload(key)
            for key in only_projection[:example_limit]
        ],
        "only_replay_examples": [
            _candle_identity_payload(key) for key in only_replay[:example_limit]
        ],
        "changed_examples": [
            {
                "identity": _candle_identity_payload(key),
                "fields": {
                    field: {
                        "projection": projection_value,
                        "replay": replay_value,
                    }
                    for (
                        field,
                        projection_value,
                        replay_value,
                    ) in _different_candle_fields(
                        projection_by[key], replay_by[key]
                    )
                },
            }
            for key in changed[:example_limit]
        ],
        "example_limit": example_limit,
    }


def _candle_identity(candle: Candle) -> tuple[str, str, int, datetime]:
    return (
        candle.exchange.value,
        candle.symbol_token,
        int(candle.interval),
        candle.start_at,
    )


def _candle_identity_payload(
    identity: tuple[str, str, int, datetime],
) -> dict[str, object]:
    exchange, symbol_token, interval_minutes, start_at = identity
    return {
        "exchange": exchange,
        "symbol_token": symbol_token,
        "interval_minutes": interval_minutes,
        "start_at": start_at.isoformat(),
    }


def _candle_field_values(candle: Candle) -> dict[str, object]:
    return {
        "trading_symbol": candle.trading_symbol,
        "end_at": candle.end_at.isoformat(),
        "open_price": str(candle.open_price),
        "high_price": str(candle.high_price),
        "low_price": str(candle.low_price),
        "close_price": str(candle.close_price),
        "volume": candle.volume,
        "tick_count": candle.tick_count,
        "completed": candle.completed,
        "trusted": candle.trusted,
        "provenance": candle.provenance.value,
    }


def _different_candle_fields(
    projection: Candle, replay: Candle
) -> tuple[tuple[str, object, object], ...]:
    projection_values = _candle_field_values(projection)
    replay_values = _candle_field_values(replay)
    return tuple(
        (field, projection_values[field], replay_values[field])
        for field in projection_values
        if projection_values[field] != replay_values[field]
    )


def _scanner_comparison(result) -> dict[str, object]:
    payload = result.to_dict()
    return {
        key: payload[key]
        for key in (
            "readiness",
            "market_regime",
            "long_candidates",
            "short_candidates",
            "qualified_long_signals",
            "qualified_short_signals",
            "evaluated_count",
            "rejected_count",
            "unhealthy_count",
            "diagnostics",
        )
    }


def _print_comparison_payload(
    payload: dict[str, object], *, json_output: bool
) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(
        "Projection/replay comparison: "
        f"date={payload['date']} equivalent={payload['equivalent']} "
        f"as_of={payload['as_of']}"
    )
    for mismatch in payload.get("mismatches", []):
        print(f"  mismatch={mismatch}")
    candle_difference = payload.get("candle_difference")
    if isinstance(candle_difference, dict):
        print(
            "  candles="
            f"projection:{candle_difference['projection_count']} "
            f"replay:{candle_difference['replay_count']} "
            f"only_projection:{candle_difference['only_projection_count']} "
            f"only_replay:{candle_difference['only_replay_count']} "
            f"changed:{candle_difference['changed_count']}"
        )
    print("Order placement available=False")


def _scan(settings: Settings, *, json_output: bool, limit: int) -> int:
    now = _ist_now()
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
        if benchmark is not None and benchmark.instrument_type != "AMXIDX":
            benchmark = None
    except (InstrumentMasterError, IntradayUniverseError, Nifty500CacheError) as exc:
        payload = {
            "readiness": "NOT_READY",
            "market_regime": "UNKNOWN",
            "diagnostics": {
                "expected_eligibility_exclusions": [],
                "reference_failures": [type(exc).__name__],
                "unhealthy_market_data": [],
                "indicator_not_ready": [],
                "benchmark_failures": [],
                "scaling_warnings": [],
            },
            "analysis_only": True,
            "long_candidates": [],
            "short_candidates": [],
            "qualified_long_signals": [],
            "qualified_short_signals": [],
            "qualified_long_count": 0,
            "qualified_short_count": 0,
            "requested_count": 0,
            "eligible_count": 0,
            "expected_ineligible_count": 0,
            "mapping_failure_count": 1,
            "indicator_not_ready_count": 0,
            "benchmark_failure_count": 0,
            "evaluated_count": 0,
            "rejected_count": 0,
            "unhealthy_count": 0,
            "raw_ticks_replayed": 0,
            "replay_elapsed_seconds": 0.0,
            "state_source": "INCREMENTAL_PROJECTION",
            "projection_age_seconds": None,
            "projection_load_seconds": 0.0,
            "incremental_ticks_processed": 0,
            "full_replay_used": False,
            "projection_last_tick_id": None,
        }
        _print_scan_payload(payload, json_output=json_output, limit=limit)
        return 2

    with _recorder(settings) as recorder:
        projection_started = perf_counter()
        try:
            view = recorder.load_projection_view(now.date())
        except ProjectionError as exc:
            payload = _projection_not_ready_payload(
                settings=settings,
                nifty_count=len(nifty_cache.constituents),
                universe=universe,
                reason=f"{type(exc).__name__}:{exc}",
                load_seconds=perf_counter() - projection_started,
            )
            _print_scan_payload(payload, json_output=json_output, limit=limit)
            return 2
        projection_load_seconds = perf_counter() - projection_started
        freshness_now = _ist_now()
        projection_age = _projection_age_seconds(
            now=freshness_now,
            last_received_at=view.checkpoint.last_received_at,
        )
        evaluation_at = view.checkpoint.updated_at
        freshness_reason = _projection_freshness_reason(
            now=freshness_now,
            projection_age_seconds=projection_age,
            last_received_at=view.checkpoint.last_received_at,
            checkpoint_updated_at=view.checkpoint.updated_at,
            stale_after_seconds=settings.scanner_projection_stale_after_seconds,
        )
        if freshness_reason is not None:
            payload = _projection_not_ready_payload(
                settings=settings,
                nifty_count=len(nifty_cache.constituents),
                universe=universe,
                reason=freshness_reason,
                load_seconds=projection_load_seconds,
                projection_age_seconds=projection_age,
                last_tick_id=view.checkpoint.last_tick_id,
            )
            _print_scan_payload(payload, json_output=json_output, limit=limit)
            return 2
        historical_start = datetime.combine(
            evaluation_at.date()
            - timedelta(days=settings.historical_bootstrap_lookback_days),
            time.min,
            tzinfo=IST,
        )
        session_start = datetime.combine(
            evaluation_at.date(), time.min, tzinfo=IST
        )
        prior_candles = recorder.load_candles(
            start_at=historical_start,
            end_at=session_start,
        )
        candles_by = _candles_by_instrument((*prior_candles, *view.candles))
        health_by = {
            (health.exchange, health.symbol_token): health for health in view.health
        }
        scanner = _market_scanner(settings)
        scaling_warnings: list[str] = []
        if projection_load_seconds > settings.scan_replay_warning_seconds:
            scaling_warnings.append(
                "projection_load_runtime_warning_threshold_exceeded:"
                f"{projection_load_seconds:.6f}>"
                f"{settings.scan_replay_warning_seconds}"
            )
        result = scanner.scan(
            instruments=universe.instruments,
            candles_by_instrument=candles_by,
            health_by_instrument=health_by,
            benchmark=benchmark,
            baseline_store=recorder,
            now=evaluation_at,
            expected_eligibility_diagnostics=[
                f"{item.symbol}:{item.reason}"
                for item in universe.expected_eligibility_exclusions
            ],
            reference_diagnostics=[
                f"{item.symbol}:{item.reason}" for item in universe.mapping_failures
            ],
            requested_count=len(settings.configured_scanner_symbols())
            or len(nifty_cache.constituents),
            raw_ticks_replayed=0,
            replay_elapsed_seconds=0.0,
            scaling_warnings=scaling_warnings,
            session_references={
                (item.exchange, item.symbol_token): item
                for item in view.session_references
            },
            opening_range_references={
                (item.exchange, item.symbol_token): item
                for item in view.opening_range_references
            },
            state_source="INCREMENTAL_PROJECTION",
            projection_age_seconds=(
                None if projection_age is None else round(projection_age, 6)
            ),
            projection_load_seconds=round(projection_load_seconds, 6),
            incremental_ticks_processed=0,
            full_replay_used=False,
            projection_last_tick_id=view.checkpoint.last_tick_id,
        )
    payload = result.to_dict()
    _print_scan_payload(payload, json_output=json_output, limit=limit)
    return 0 if result.readiness.value == "READY" else 2


def _projection_not_ready_payload(
    *,
    settings: Settings,
    nifty_count: int,
    universe,
    reason: str,
    load_seconds: float,
    projection_age_seconds: float | None = None,
    last_tick_id: int | None = None,
    incremental_ticks_processed: int = 0,
) -> dict[str, object]:
    return {
        "readiness": "NOT_READY",
        "market_regime": "UNKNOWN",
        "diagnostics": {
            "expected_eligibility_exclusions": [],
            "reference_failures": [],
            "unhealthy_market_data": [],
            "indicator_not_ready": [],
            "benchmark_failures": [reason],
            "scaling_warnings": [],
        },
        "analysis_only": True,
        "long_candidates": [],
        "short_candidates": [],
        "qualified_long_signals": [],
        "qualified_short_signals": [],
        "qualified_long_count": 0,
        "qualified_short_count": 0,
        "requested_count": len(settings.configured_scanner_symbols()) or nifty_count,
        "eligible_count": len(universe.instruments),
        "expected_ineligible_count": len(
            universe.expected_eligibility_exclusions
        ),
        "mapping_failure_count": len(universe.mapping_failures),
        "indicator_not_ready_count": 0,
        "benchmark_failure_count": 1,
        "evaluated_count": 0,
        "rejected_count": 0,
        "unhealthy_count": 0,
        "raw_ticks_replayed": 0,
        "replay_elapsed_seconds": 0.0,
        "state_source": "INCREMENTAL_PROJECTION",
        "projection_age_seconds": (
            None
            if projection_age_seconds is None
            else round(projection_age_seconds, 6)
        ),
        "projection_load_seconds": round(load_seconds, 6),
        "incremental_ticks_processed": incremental_ticks_processed,
        "full_replay_used": False,
        "projection_last_tick_id": last_tick_id,
    }


def _market_expected_live(now: datetime) -> bool:
    local_time = now.astimezone(IST).timetz().replace(tzinfo=None)
    return time(9, 15) <= local_time < time(15, 30)


def _projection_freshness_reason(
    *,
    now: datetime,
    projection_age_seconds: float | None,
    last_received_at: datetime | None,
    checkpoint_updated_at: datetime | None = None,
    stale_after_seconds: float,
) -> str | None:
    if _projection_timestamp_is_future(
        now=now,
        last_received_at=last_received_at,
        checkpoint_updated_at=checkpoint_updated_at,
    ):
        return "projection_timestamp_in_future"
    if not _market_expected_live(now):
        return None
    if (
        projection_age_seconds is None
        or projection_age_seconds > stale_after_seconds
    ):
        return f"projection_stale:{projection_age_seconds}>{stale_after_seconds}"
    return None


def _projection_timestamp_is_future(
    *,
    now: datetime,
    last_received_at: datetime | None,
    checkpoint_updated_at: datetime | None,
) -> bool:
    return any(
        timestamp is not None and timestamp > now
        for timestamp in (last_received_at, checkpoint_updated_at)
    )


def _projection_age_seconds(
    *, now: datetime, last_received_at: datetime | None
) -> float | None:
    if last_received_at is None:
        return None
    return max(0.0, (now - last_received_at).total_seconds())


def _ist_now() -> datetime:
    return datetime.now(IST)


def _market_scanner(settings: Settings) -> MarketScanner:
    return MarketScanner(
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
        regime_momentum_threshold_pct=settings.regime_momentum_threshold_pct,
        regime_slope_lookback_candles=settings.regime_slope_lookback_candles,
        max_vwap_distance_atr=settings.max_vwap_distance_atr,
    )


def _print_scan_payload(
    payload: dict[str, object], *, json_output: bool, limit: int
) -> None:
    if json_output:
        limited = dict(payload)
        limited["long_candidates"] = list(payload.get("long_candidates", []))[:limit]
        limited["short_candidates"] = list(payload.get("short_candidates", []))[:limit]
        limited["qualified_long_signals"] = list(
            payload.get("qualified_long_signals", [])
        )[:limit]
        limited["qualified_short_signals"] = list(
            payload.get("qualified_short_signals", [])
        )[:limit]
        print(json.dumps(limited, indent=2, sort_keys=True))
        return
    print(
        f"Scanner readiness={payload['readiness']} "
        f"regime={payload['market_regime']} analysis_only=True"
    )
    print(
        f"requested={payload.get('requested_count', 0)} "
        f"eligible={payload.get('eligible_count', 0)} "
        f"expected_ineligible={payload.get('expected_ineligible_count', 0)} "
        f"mapping_failures={payload.get('mapping_failure_count', 0)} "
        f"qualified_long={payload.get('qualified_long_count', 0)} "
        f"qualified_short={payload.get('qualified_short_count', 0)} "
        f"raw_ticks_replayed={payload.get('raw_ticks_replayed', 0)} "
        f"replay_elapsed_seconds={payload.get('replay_elapsed_seconds', 0.0)} "
        f"state_source={payload.get('state_source', 'UNKNOWN')} "
        f"projection_age_seconds={payload.get('projection_age_seconds')} "
        f"projection_load_seconds={payload.get('projection_load_seconds', 0.0)} "
        f"incremental_ticks_processed="
        f"{payload.get('incremental_ticks_processed', 0)} "
        f"full_replay_used={payload.get('full_replay_used', False)}"
    )
    print("BIAS  SYMBOL              SCORE QUAL      LTP     RVOL       RS")
    for bias_key in ("long_candidates", "short_candidates"):
        for item in list(payload.get(bias_key, []))[:limit]:
            print(
                f"{item['bias']:<5} {item['symbol']:<19} "
                f"{item['total_score']:>5} "
                f"{str(item.get('qualified', False)):<5} {item['ltp']:>8} "
                f"{item['relative_volume']:>8} {item['relative_strength']:>8}"
            )
    raw_diagnostics = payload.get("diagnostics", {})
    if isinstance(raw_diagnostics, dict):
        diagnostics = [
            f"{category}:{reason}"
            for category, reasons in sorted(raw_diagnostics.items())
            for reason in list(reasons)
        ]
    else:
        diagnostics = list(raw_diagnostics)
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
    )


def _projection(
    settings: Settings,
    recorder: SQLiteMarketRecorder,
    session_date: date,
) -> MarketIntelligenceProjection:
    return MarketIntelligenceProjection(
        recorder,
        session_date=session_date,
        stale_after_seconds=settings.market_data_stale_after_seconds,
        recovery_consecutive_ticks=settings.market_data_recovery_consecutive_ticks,
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
