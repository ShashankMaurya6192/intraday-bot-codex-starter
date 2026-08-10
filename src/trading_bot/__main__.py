from __future__ import annotations

import argparse
from collections.abc import Sequence

from .broker import AngelCredentials, AngelOneMarketDataAdapter, ReconnectPolicy
from .config import Settings
from .storage import SQLiteMarketRecorder


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot",
        description="Paper-first Angel One intraday market-data recorder",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="show redacted Phase 1 configuration status")
    subparsers.add_parser("init-db", help="create or migrate the SQLite recorder")
    subparsers.add_parser(
        "record-market",
        help="authenticate and record Angel One WebSocket market data (no orders)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings()

    # Phase 1 intentionally has no explicit-live confirmation path. Any LIVE
    # configuration therefore fails closed before a command runs.
    settings.assert_live_allowed()
    command = args.command or "status"

    if command == "status":
        subscriptions = settings.market_subscriptions()
        print(f"Intraday Bot | mode={settings.trading_mode.value}")
        print(f"Angel credentials configured={settings.angel_credentials_configured}")
        print(f"Market subscriptions={len(subscriptions)}")
        print(f"Database={settings.database_url}")
        print("Order placement available=False")
        return 0

    if command == "init-db":
        with SQLiteMarketRecorder(
            settings.database_url,
            busy_timeout_ms=settings.database_busy_timeout_ms,
        ):
            pass
        print(f"Initialized SQLite market recorder at {settings.database_url}")
        return 0

    if command == "record-market":
        if not settings.angel_credentials_configured:
            raise RuntimeError(
                "Angel One credentials are incomplete; populate the local .env file"
            )
        subscriptions = settings.market_subscriptions()
        if not subscriptions:
            raise RuntimeError(
                "ANGEL_MARKET_SUBSCRIPTIONS is empty; no market data can be recorded"
            )

        credentials = AngelCredentials(
            api_key=settings.angel_api_key.get_secret_value(),
            client_code=settings.angel_client_code.get_secret_value(),
            password=settings.angel_password.get_secret_value(),
            totp_secret=settings.angel_totp_secret.get_secret_value(),
        )
        policy = ReconnectPolicy(
            max_attempts=settings.websocket_max_reconnect_attempts,
            initial_delay_seconds=settings.websocket_reconnect_initial_delay_seconds,
            multiplier=settings.websocket_reconnect_multiplier,
            max_delay_seconds=settings.websocket_reconnect_max_delay_seconds,
        )
        adapter = AngelOneMarketDataAdapter(
            credentials,
            subscription_mode=settings.angel_subscription_mode,
            correlation_id=settings.angel_correlation_id,
            reconnect_policy=policy,
        )
        with SQLiteMarketRecorder(
            settings.database_url,
            busy_timeout_ms=settings.database_busy_timeout_ms,
        ) as recorder:
            print(
                f"Recording {len(subscriptions)} subscription(s) in PAPER mode. "
                "Press Ctrl+C to stop."
            )
            try:
                adapter.stream(
                    subscriptions,
                    recorder.record_tick,
                    recorder.record_feed_event,
                )
            except KeyboardInterrupt:
                print("Stopping market recorder...")
            finally:
                adapter.disconnect()
        return 0

    raise AssertionError(f"unhandled command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
