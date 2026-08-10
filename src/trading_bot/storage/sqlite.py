"""Thread-safe SQLite recorder for normalized ticks and feed audit events."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

from trading_bot.domain import (
    IST,
    Exchange,
    FeedEvent,
    FeedEventType,
    MarketTick,
    SubscriptionMode,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol_token TEXT NOT NULL,
    trading_symbol TEXT,
    subscription_mode TEXT NOT NULL,
    sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
    exchange_timestamp TEXT NOT NULL,
    received_at TEXT NOT NULL,
    last_traded_price TEXT NOT NULL,
    last_traded_quantity INTEGER,
    average_traded_price TEXT,
    volume_trade_for_the_day INTEGER,
    total_buy_quantity TEXT,
    total_sell_quantity TEXT,
    open_price TEXT,
    high_price TEXT,
    low_price TEXT,
    close_price TEXT,
    raw_payload TEXT NOT NULL,
    UNIQUE (exchange, symbol_token, sequence_number, exchange_timestamp)
);

CREATE INDEX IF NOT EXISTS idx_market_ticks_instrument_time
    ON market_ticks (exchange, symbol_token, exchange_timestamp);
CREATE INDEX IF NOT EXISTS idx_market_ticks_received_at
    ON market_ticks (received_at);

CREATE TABLE IF NOT EXISTS market_feed_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_feed_events_time
    ON market_feed_events (occurred_at);
"""


class SQLiteMarketRecorder:
    """Persist market data synchronously before downstream processing."""

    def __init__(self, database_url: str, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms < 100:
            raise ValueError("busy_timeout_ms must be at least 100")
        database_path = sqlite_path_from_url(database_url)
        if database_path != ":memory:":
            Path(database_path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )

        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            database_path,
            timeout=busy_timeout_ms / 1_000,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO schema_version(version, applied_at)
                VALUES (?, ?)
                """,
                (1, datetime.now(IST).isoformat()),
            )
            self._connection.commit()

    def record_tick(self, tick: MarketTick) -> bool:
        """Record one tick; return False when the idempotency key already exists."""
        with self._lock, self._connection:
            cursor = self._connection.execute(_INSERT_TICK, _tick_values(tick))
            return cursor.rowcount == 1

    def record_ticks(self, ticks: Iterable[MarketTick]) -> int:
        """Record a batch atomically and return the number of newly inserted rows."""
        values = [_tick_values(tick) for tick in ticks]
        if not values:
            return 0
        inserted = 0
        with self._lock, self._connection:
            for value in values:
                cursor = self._connection.execute(_INSERT_TICK, value)
                inserted += int(cursor.rowcount == 1)
        return inserted

    def record_feed_event(self, event: FeedEvent) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO market_feed_events(
                    event_type, occurred_at, message, details
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    event.event_type.value,
                    event.occurred_at.isoformat(),
                    event.message,
                    _json(event.details),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return an event row ID")
            return int(cursor.lastrowid)

    def load_ticks(
        self,
        *,
        exchange: Exchange | None = None,
        symbol_token: str | None = None,
        limit: int | None = None,
    ) -> list[MarketTick]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        parameters: list[object] = []
        if exchange is not None:
            clauses.append("exchange = ?")
            parameters.append(exchange.value)
        if symbol_token is not None:
            clauses.append("symbol_token = ?")
            parameters.append(symbol_token)

        sql = "SELECT * FROM market_ticks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY exchange_timestamp, sequence_number"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)

        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [_row_to_tick(row) for row in rows]

    def load_feed_events(self, *, limit: int | None = None) -> list[FeedEvent]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        sql = "SELECT * FROM market_feed_events ORDER BY occurred_at, id"
        parameters: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [
            FeedEvent(
                event_type=FeedEventType(row["event_type"]),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                message=row["message"],
                details=json.loads(row["details"]),
            )
            for row in rows
        ]

    def count_ticks(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM market_ticks"
            ).fetchone()
        return int(row["count"])

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteMarketRecorder:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def sqlite_path_from_url(database_url: str) -> str:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("only sqlite:/// database URLs are supported")
    path = unquote(database_url.removeprefix(prefix)).strip()
    if not path:
        raise ValueError("SQLite database path must not be blank")
    if "?" in path or "#" in path:
        raise ValueError(
            "SQLite database URL query strings and fragments are unsupported"
        )
    return path


_INSERT_TICK = """
INSERT OR IGNORE INTO market_ticks(
    exchange,
    symbol_token,
    trading_symbol,
    subscription_mode,
    sequence_number,
    exchange_timestamp,
    received_at,
    last_traded_price,
    last_traded_quantity,
    average_traded_price,
    volume_trade_for_the_day,
    total_buy_quantity,
    total_sell_quantity,
    open_price,
    high_price,
    low_price,
    close_price,
    raw_payload
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _tick_values(tick: MarketTick) -> tuple[object, ...]:
    return (
        tick.exchange.value,
        tick.symbol_token,
        tick.trading_symbol,
        tick.subscription_mode.value,
        tick.sequence_number,
        tick.exchange_timestamp.isoformat(),
        tick.received_at.isoformat(),
        str(tick.last_traded_price),
        tick.last_traded_quantity,
        _optional_decimal_string(tick.average_traded_price),
        tick.volume_trade_for_the_day,
        _optional_decimal_string(tick.total_buy_quantity),
        _optional_decimal_string(tick.total_sell_quantity),
        _optional_decimal_string(tick.open_price),
        _optional_decimal_string(tick.high_price),
        _optional_decimal_string(tick.low_price),
        _optional_decimal_string(tick.close_price),
        _json(tick.raw_payload),
    )


def _row_to_tick(row: sqlite3.Row) -> MarketTick:
    return MarketTick(
        exchange=Exchange(row["exchange"]),
        symbol_token=row["symbol_token"],
        trading_symbol=row["trading_symbol"],
        subscription_mode=SubscriptionMode(row["subscription_mode"]),
        sequence_number=row["sequence_number"],
        exchange_timestamp=datetime.fromisoformat(row["exchange_timestamp"]),
        received_at=datetime.fromisoformat(row["received_at"]),
        last_traded_price=Decimal(row["last_traded_price"]),
        last_traded_quantity=row["last_traded_quantity"],
        average_traded_price=_optional_decimal(row["average_traded_price"]),
        volume_trade_for_the_day=row["volume_trade_for_the_day"],
        total_buy_quantity=_optional_decimal(row["total_buy_quantity"]),
        total_sell_quantity=_optional_decimal(row["total_sell_quantity"]),
        open_price=_optional_decimal(row["open_price"]),
        high_price=_optional_decimal(row["high_price"]),
        low_price=_optional_decimal(row["low_price"]),
        close_price=_optional_decimal(row["close_price"]),
        raw_payload=json.loads(row["raw_payload"]),
    )


def _optional_decimal_string(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
