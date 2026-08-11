"""Thread-safe SQLite recorder for normalized ticks and feed audit events."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

from trading_bot.broker.instruments import BrokerInstrument
from trading_bot.domain import (
    IST,
    Exchange,
    FeedEvent,
    FeedEventType,
    HistoricalProvider,
    MarketTick,
    SubscriptionMode,
)
from trading_bot.marketdata import (
    Candle,
    CandleInterval,
    InstrumentHealth,
    MarketDataHealthStatus,
    MarketDataProvenance,
    SessionReference,
)
from trading_bot.marketdata.history import HistoricalBootstrapStatus
from trading_bot.marketdata.projection import (
    ProjectionCatchUpPendingError,
    ProjectionCheckpoint,
    ProjectionCheckpointAheadError,
    ProjectionConcurrentAdvanceError,
    ProjectionCutoffBehindCheckpointError,
    ProjectionStateCorruptError,
    ProjectionUnavailableError,
    ProjectionView,
)
from trading_bot.marketdata.recovery import opening_references_visible

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

CREATE TABLE IF NOT EXISTS market_candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol_token TEXT NOT NULL,
    trading_symbol TEXT,
    interval_minutes INTEGER NOT NULL CHECK (interval_minutes IN (1, 5)),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    open_price TEXT NOT NULL,
    high_price TEXT NOT NULL,
    low_price TEXT NOT NULL,
    close_price TEXT NOT NULL,
    volume INTEGER NOT NULL CHECK (volume >= 0),
    tick_count INTEGER NOT NULL CHECK (tick_count > 0),
    completed INTEGER NOT NULL CHECK (completed IN (0, 1)),
    trusted INTEGER NOT NULL CHECK (trusted IN (0, 1)),
    provenance TEXT NOT NULL DEFAULT 'LOCAL_TICKS',
    UNIQUE (exchange, symbol_token, interval_minutes, start_at)
);

CREATE INDEX IF NOT EXISTS idx_market_candles_instrument_time
    ON market_candles (exchange, symbol_token, interval_minutes, start_at);

CREATE TABLE IF NOT EXISTS relative_volume_baselines (
    exchange TEXT NOT NULL,
    symbol_token TEXT NOT NULL,
    session_date TEXT NOT NULL,
    minute_offset INTEGER NOT NULL CHECK (minute_offset BETWEEN 0 AND 374),
    cumulative_volume INTEGER NOT NULL CHECK (cumulative_volume >= 0),
    PRIMARY KEY (exchange, symbol_token, session_date, minute_offset)
);

CREATE INDEX IF NOT EXISTS idx_rvol_baseline_lookup
    ON relative_volume_baselines (
        exchange, symbol_token, minute_offset, session_date DESC
    );

CREATE TABLE IF NOT EXISTS historical_bootstrap_status (
    exchange TEXT NOT NULL,
    symbol_token TEXT NOT NULL,
    trading_symbol TEXT NOT NULL,
    from_date TEXT NOT NULL,
    through_date TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    sessions_found INTEGER NOT NULL CHECK (sessions_found > 0),
    one_minute_candles INTEGER NOT NULL CHECK (one_minute_candles > 0),
    five_minute_candles INTEGER NOT NULL CHECK (five_minute_candles > 0),
    source_sha256 TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'angel',
    provenance TEXT NOT NULL DEFAULT 'ANGEL_HISTORICAL',
    PRIMARY KEY (exchange, symbol_token)
);

CREATE TABLE IF NOT EXISTS market_health_snapshot (
    session_date TEXT NOT NULL,
    exchange TEXT NOT NULL,
    symbol_token TEXT NOT NULL,
    feed_epoch INTEGER NOT NULL CHECK (feed_epoch >= 0),
    status TEXT NOT NULL,
    latest_exchange_timestamp TEXT,
    latest_received_timestamp TEXT,
    sequence_number INTEGER,
    data_age_seconds REAL,
    duplicate_count INTEGER NOT NULL CHECK (duplicate_count >= 0),
    out_of_order_count INTEGER NOT NULL CHECK (out_of_order_count >= 0),
    sequence_jump_count INTEGER NOT NULL CHECK (sequence_jump_count >= 0),
    last_health_transition TEXT NOT NULL,
    PRIMARY KEY (session_date, exchange, symbol_token)
);

CREATE TABLE IF NOT EXISTS market_session_reference (
    session_date TEXT NOT NULL,
    reference_kind TEXT NOT NULL CHECK (
        reference_kind IN ('LATEST', 'BEFORE_FIRST_FIVE', 'BEFORE_OPENING_RANGE')
    ),
    exchange TEXT NOT NULL,
    symbol_token TEXT NOT NULL,
    trading_symbol TEXT,
    observed_at TEXT NOT NULL,
    session_open TEXT NOT NULL,
    session_high TEXT NOT NULL,
    session_low TEXT NOT NULL,
    last_traded_price TEXT NOT NULL,
    average_traded_price TEXT,
    cumulative_volume INTEGER,
    provenance TEXT NOT NULL,
    trusted INTEGER NOT NULL CHECK (trusted IN (0, 1)),
    PRIMARY KEY (session_date, reference_kind, exchange, symbol_token)
);

CREATE TABLE IF NOT EXISTS market_projection_checkpoint (
    session_date TEXT PRIMARY KEY,
    last_tick_id INTEGER NOT NULL CHECK (last_tick_id >= 0),
    last_feed_event_id INTEGER NOT NULL CHECK (last_feed_event_id >= 0),
    last_received_at TEXT,
    last_feed_event_at TEXT,
    feed_epoch INTEGER NOT NULL CHECK (feed_epoch >= 0),
    updated_at TEXT NOT NULL,
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    state_json TEXT NOT NULL
);
"""


class SQLiteMarketRecorder:
    """Persist ticks, completed candles, feed events, and RVOL baselines."""

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
            _ensure_column(
                self._connection,
                table="market_candles",
                column="provenance",
                definition="TEXT NOT NULL DEFAULT 'LOCAL_TICKS'",
            )
            _ensure_column(
                self._connection,
                table="historical_bootstrap_status",
                column="provider",
                definition="TEXT NOT NULL DEFAULT 'angel'",
            )
            _ensure_column(
                self._connection,
                table="historical_bootstrap_status",
                column="provenance",
                definition="TEXT NOT NULL DEFAULT 'ANGEL_HISTORICAL'",
            )
            self._connection.execute(
                """
                UPDATE market_candles
                SET provenance = 'ANGEL_HISTORICAL'
                WHERE provenance = 'LOCAL_TICKS'
                  AND EXISTS (
                    SELECT 1
                    FROM historical_bootstrap_status AS history
                    WHERE history.exchange = market_candles.exchange
                      AND history.symbol_token = market_candles.symbol_token
                      AND date(market_candles.start_at) >= history.from_date
                      AND date(market_candles.start_at) <= history.through_date
                  )
                """
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO schema_version(version, applied_at)
                VALUES (?, ?)
                """,
                (1, datetime.now(IST).isoformat()),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO schema_version(version, applied_at)
                VALUES (?, ?)
                """,
                (2, datetime.now(IST).isoformat()),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO schema_version(version, applied_at)
                VALUES (?, ?)
                """,
                (3, datetime.now(IST).isoformat()),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO schema_version(version, applied_at)
                VALUES (?, ?)
                """,
                (4, datetime.now(IST).isoformat()),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO schema_version(version, applied_at)
                VALUES (?, ?)
                """,
                (5, datetime.now(IST).isoformat()),
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

    def record_ticks_with_ids(
        self, ticks: Iterable[MarketTick]
    ) -> list[tuple[int, MarketTick]]:
        """Atomically persist a batch and return only newly inserted row identities."""
        items = list(ticks)
        if not items:
            return []
        inserted: list[tuple[int, MarketTick]] = []
        with self._lock, self._connection:
            for tick in items:
                cursor = self._connection.execute(_INSERT_TICK, _tick_values(tick))
                if cursor.rowcount == 1:
                    if cursor.lastrowid is None:
                        raise RuntimeError("SQLite did not return a tick row ID")
                    inserted.append((int(cursor.lastrowid), tick))
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
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        received_at_or_before: datetime | None = None,
        received_order: bool = False,
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
        if start_at is not None:
            clauses.append("exchange_timestamp >= ?")
            parameters.append(_aware_iso(start_at))
        if end_at is not None:
            clauses.append("exchange_timestamp < ?")
            parameters.append(_aware_iso(end_at))
        if received_at_or_before is not None:
            clauses.append("received_at <= ?")
            parameters.append(_aware_iso(received_at_or_before))

        sql = "SELECT * FROM market_ticks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if received_order:
            sql += " ORDER BY received_at, id"
        else:
            sql += " ORDER BY exchange_timestamp, sequence_number"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)

        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [_row_to_tick(row) for row in rows]

    def load_tick_records_after(
        self,
        session_date: date,
        *,
        after_id: int,
        received_at_or_before: datetime,
        limit: int | None = None,
    ) -> list[tuple[int, MarketTick]]:
        if after_id < 0:
            raise ValueError("after_id must be non-negative")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        start_at, end_at = _session_iso_bounds(session_date)
        sql = """
            SELECT * FROM market_ticks
            WHERE id > ? AND exchange_timestamp >= ? AND exchange_timestamp < ?
            ORDER BY id
        """
        parameters: list[object] = [after_id, start_at, end_at]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        cutoff = datetime.fromisoformat(_aware_iso(received_at_or_before))
        visible: list[tuple[int, MarketTick]] = []
        for row in rows:
            tick = _row_to_tick(row)
            if tick.received_at > cutoff:
                break
            visible.append((int(row["id"]), tick))
        return visible

    def max_tick_id(self, session_date: date) -> int:
        start_at, end_at = _session_iso_bounds(session_date)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COALESCE(MAX(id), 0) AS max_id
                FROM market_ticks
                WHERE exchange_timestamp >= ? AND exchange_timestamp < ?
                """,
                (start_at, end_at),
            ).fetchone()
        return int(row["max_id"])

    def record_candles(self, candles: Iterable[Candle]) -> int:
        """Persist completed candles idempotently in one transaction."""
        values = [_candle_values(candle) for candle in candles]
        if not values:
            return 0
        inserted = 0
        with self._lock, self._connection:
            for value in values:
                cursor = self._connection.execute(_INSERT_CANDLE, value)
                inserted += int(cursor.rowcount == 1)
        return inserted

    def load_candles(
        self,
        *,
        exchange: Exchange | None = None,
        symbol_token: str | None = None,
        interval: CandleInterval | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> list[Candle]:
        clauses: list[str] = []
        parameters: list[object] = []
        if exchange is not None:
            clauses.append("exchange = ?")
            parameters.append(exchange.value)
        if symbol_token is not None:
            clauses.append("symbol_token = ?")
            parameters.append(symbol_token)
        if interval is not None:
            clauses.append("interval_minutes = ?")
            parameters.append(int(interval))
        if start_at is not None:
            clauses.append("start_at >= ?")
            parameters.append(_aware_iso(start_at))
        if end_at is not None:
            clauses.append("start_at < ?")
            parameters.append(_aware_iso(end_at))
        sql = "SELECT * FROM market_candles"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY start_at, interval_minutes, exchange, symbol_token"
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [_row_to_candle(row) for row in rows]

    def record_volume_baseline_session(
        self,
        *,
        exchange: Exchange,
        symbol_token: str,
        session_date: date,
        cumulative_by_minute: dict[int, int],
        complete: bool,
        trusted: bool,
    ) -> bool:
        """Store only complete/trusted sessions for like-for-like RVOL."""
        token = symbol_token.strip()
        if not token:
            raise ValueError("symbol_token must not be blank")
        previous = -1
        for minute, cumulative in sorted(cumulative_by_minute.items()):
            if not 0 <= minute <= 374:
                raise ValueError("baseline minute must be within the NSE session")
            if cumulative < previous:
                raise ValueError("baseline cumulative volume must be non-decreasing")
            previous = cumulative
        with self._lock, self._connection:
            self._connection.execute(
                """
                DELETE FROM relative_volume_baselines
                WHERE exchange = ? AND symbol_token = ? AND session_date = ?
                """,
                (exchange.value, token, session_date.isoformat()),
            )
            if not complete or not trusted or not cumulative_by_minute:
                return False
            self._connection.executemany(
                """
                INSERT INTO relative_volume_baselines(
                    exchange, symbol_token, session_date,
                    minute_offset, cumulative_volume
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        exchange.value,
                        token,
                        session_date.isoformat(),
                        minute,
                        cumulative,
                    )
                    for minute, cumulative in sorted(cumulative_by_minute.items())
                ],
            )
        return True

    def load_rvol_baselines(
        self,
        *,
        exchange: Exchange,
        symbol_token: str,
        before_date: date,
        minute_offset: int,
        lookback_sessions: int,
    ) -> tuple[int, ...]:
        if not 0 <= minute_offset <= 374:
            raise ValueError("minute_offset must be within the NSE session")
        if lookback_sessions < 1:
            raise ValueError("lookback_sessions must be positive")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT cumulative_volume
                FROM relative_volume_baselines
                WHERE exchange = ?
                  AND symbol_token = ?
                  AND session_date < ?
                  AND minute_offset = ?
                ORDER BY session_date DESC
                LIMIT ?
                """,
                (
                    exchange.value,
                    symbol_token,
                    before_date.isoformat(),
                    minute_offset,
                    lookback_sessions,
                ),
            ).fetchall()
        return tuple(int(row["cumulative_volume"]) for row in rows)

    def load_historical_bootstrap_status(
        self, *, exchange: Exchange, symbol_token: str
    ) -> HistoricalBootstrapStatus | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM historical_bootstrap_status
                WHERE exchange = ? AND symbol_token = ?
                """,
                (exchange.value, symbol_token),
            ).fetchone()
        if row is None:
            return None
        return HistoricalBootstrapStatus(
            exchange=Exchange(row["exchange"]),
            symbol_token=row["symbol_token"],
            trading_symbol=row["trading_symbol"],
            from_date=date.fromisoformat(row["from_date"]),
            through_date=date.fromisoformat(row["through_date"]),
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            sessions_found=row["sessions_found"],
            one_minute_candles=row["one_minute_candles"],
            five_minute_candles=row["five_minute_candles"],
            source_sha256=row["source_sha256"],
            provider=row["provider"],
            provenance=MarketDataProvenance(row["provenance"]),
        )

    def replace_historical_dataset(
        self,
        *,
        instrument: BrokerInstrument,
        one_minute_candles: Sequence[Candle],
        five_minute_candles: Sequence[Candle],
        complete_session_dates: Sequence[date],
        from_date: date,
        through_date: date,
        fetched_at: datetime,
        source_sha256: str,
        build_rvol_baselines: bool,
        provider: str = "angel",
        provenance: MarketDataProvenance = MarketDataProvenance.ANGEL_HISTORICAL,
    ) -> HistoricalBootstrapStatus:
        """Atomically replace one fully validated broker-history dataset."""
        if not one_minute_candles or not five_minute_candles:
            raise ValueError("historical dataset must contain 1m and 5m candles")
        actual_provider = HistoricalProvider(provider)
        if actual_provider is HistoricalProvider.AUTO:
            raise ValueError("persisted historical provider must be concrete")
        provenance = MarketDataProvenance(provenance)
        expected_provenance = (
            MarketDataProvenance.YFINANCE
            if actual_provider is HistoricalProvider.YFINANCE
            else MarketDataProvenance.ANGEL_HISTORICAL
        )
        if provenance is not expected_provenance:
            raise ValueError("historical provider/provenance are inconsistent")
        if not complete_session_dates or from_date > through_date:
            raise ValueError("historical session/date bounds are invalid")
        if len(source_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in source_sha256.lower()
        ):
            raise ValueError("source_sha256 must be a SHA-256 digest")
        fetched_iso = _aware_iso(fetched_at)
        session_dates = tuple(sorted(set(complete_session_dates)))
        if any(not from_date <= item <= through_date for item in session_dates):
            raise ValueError("historical sessions fall outside requested date bounds")
        all_candles = tuple(one_minute_candles) + tuple(five_minute_candles)
        for candle in all_candles:
            if (
                candle.exchange is not instrument.exchange
                or candle.symbol_token != instrument.symbol_token
                or candle.start_at.date() not in session_dates
                or not candle.completed
                or not candle.trusted
                or candle.provenance is not provenance
            ):
                raise ValueError("historical candle identity/trust is inconsistent")
        range_start = datetime.combine(from_date, time.min, tzinfo=IST).isoformat()
        range_end = datetime.combine(
            through_date + timedelta(days=1), time.min, tzinfo=IST
        ).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """
                DELETE FROM market_candles
                WHERE exchange = ? AND symbol_token = ?
                  AND start_at >= ? AND start_at < ?
                """,
                (
                    instrument.exchange.value,
                    instrument.symbol_token,
                    range_start,
                    range_end,
                ),
            )
            for candle in all_candles:
                self._connection.execute(_INSERT_CANDLE, _candle_values(candle))
            self._connection.execute(
                """
                DELETE FROM relative_volume_baselines
                WHERE exchange = ? AND symbol_token = ?
                  AND session_date >= ? AND session_date <= ?
                """,
                (
                    instrument.exchange.value,
                    instrument.symbol_token,
                    from_date.isoformat(),
                    through_date.isoformat(),
                ),
            )
            if build_rvol_baselines:
                grouped: dict[date, list[Candle]] = {}
                for candle in one_minute_candles:
                    grouped.setdefault(candle.start_at.date(), []).append(candle)
                baseline_rows: list[tuple[object, ...]] = []
                for session_date in session_dates:
                    items = sorted(
                        grouped.get(session_date, ()), key=lambda item: item.start_at
                    )
                    if len(items) != 375:
                        raise ValueError("RVOL history requires complete 1m sessions")
                    cumulative = 0
                    for minute_offset, candle in enumerate(items):
                        cumulative += candle.volume
                        baseline_rows.append(
                            (
                                instrument.exchange.value,
                                instrument.symbol_token,
                                session_date.isoformat(),
                                minute_offset,
                                cumulative,
                            )
                        )
                self._connection.executemany(
                    """
                    INSERT INTO relative_volume_baselines(
                        exchange, symbol_token, session_date,
                        minute_offset, cumulative_volume
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    baseline_rows,
                )
            self._connection.execute(
                """
                INSERT INTO historical_bootstrap_status(
                    exchange, symbol_token, trading_symbol, from_date,
                    through_date, fetched_at, sessions_found,
                    one_minute_candles, five_minute_candles, source_sha256,
                    provider, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, symbol_token) DO UPDATE SET
                    trading_symbol = excluded.trading_symbol,
                    from_date = excluded.from_date,
                    through_date = excluded.through_date,
                    fetched_at = excluded.fetched_at,
                    sessions_found = excluded.sessions_found,
                    one_minute_candles = excluded.one_minute_candles,
                    five_minute_candles = excluded.five_minute_candles,
                    source_sha256 = excluded.source_sha256,
                    provider = excluded.provider,
                    provenance = excluded.provenance
                """,
                (
                    instrument.exchange.value,
                    instrument.symbol_token,
                    instrument.trading_symbol,
                    from_date.isoformat(),
                    through_date.isoformat(),
                    fetched_iso,
                    len(session_dates),
                    len(one_minute_candles),
                    len(five_minute_candles),
                    source_sha256.lower(),
                    actual_provider.value,
                    provenance.value,
                ),
            )
        status = self.load_historical_bootstrap_status(
            exchange=instrument.exchange,
            symbol_token=instrument.symbol_token,
        )
        if status is None:
            raise RuntimeError("historical bootstrap status was not persisted")
        return status

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

    def load_feed_event_records_after(
        self,
        session_date: date,
        *,
        after_id: int,
        occurred_at_or_before: datetime,
    ) -> list[tuple[int, FeedEvent]]:
        if after_id < 0:
            raise ValueError("after_id must be non-negative")
        start_at, end_at = _session_iso_bounds(session_date)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM market_feed_events
                WHERE id > ? AND occurred_at >= ? AND occurred_at < ?
                ORDER BY id
                """,
                (after_id, start_at, end_at),
            ).fetchall()
        cutoff = datetime.fromisoformat(_aware_iso(occurred_at_or_before))
        visible: list[tuple[int, FeedEvent]] = []
        for row in rows:
            event = FeedEvent(
                event_type=FeedEventType(row["event_type"]),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                message=row["message"],
                details=json.loads(row["details"]),
            )
            if event.occurred_at > cutoff:
                break
            visible.append((int(row["id"]), event))
        return visible

    def max_feed_event_id(self, session_date: date) -> int:
        start_at, end_at = _session_iso_bounds(session_date)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COALESCE(MAX(id), 0) AS max_id
                FROM market_feed_events
                WHERE occurred_at >= ? AND occurred_at < ?
                """,
                (start_at, end_at),
            ).fetchone()
        return int(row["max_id"])

    def load_projection_checkpoint(
        self, session_date: date
    ) -> ProjectionCheckpoint | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM market_projection_checkpoint
                WHERE session_date = ?
                """,
                (session_date.isoformat(),),
            ).fetchone()
        if row is None:
            return None
        return _row_to_projection_checkpoint(row)

    def load_projection_health(self, session_date: date) -> list[InstrumentHealth]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM market_health_snapshot
                WHERE session_date = ?
                ORDER BY exchange, symbol_token
                """,
                (session_date.isoformat(),),
            ).fetchall()
        return [_row_to_instrument_health(row) for row in rows]

    def load_projection_references(
        self, session_date: date, *, reference_kind: str
    ) -> list[SessionReference]:
        if reference_kind not in {
            "LATEST",
            "BEFORE_FIRST_FIVE",
            "BEFORE_OPENING_RANGE",
        }:
            raise ValueError("unsupported projection reference kind")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM market_session_reference
                WHERE session_date = ? AND reference_kind = ?
                ORDER BY exchange, symbol_token
                """,
                (session_date.isoformat(), reference_kind),
            ).fetchall()
        return [_row_to_session_reference(row) for row in rows]

    def load_projection_view(
        self, session_date: date, *, now: datetime | None = None
    ) -> ProjectionView:
        """Load one consistent commit, optionally enforcing an explicit cutoff."""
        evaluation = (
            None
            if now is None
            else datetime.fromisoformat(_aware_iso(now))
        )
        session_text = session_date.isoformat()
        start_at, end_at = _session_iso_bounds(session_date)
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                checkpoint_row = self._connection.execute(
                    """
                    SELECT * FROM market_projection_checkpoint
                    WHERE session_date = ?
                    """,
                    (session_text,),
                ).fetchone()
                health_rows = self._connection.execute(
                    """
                    SELECT * FROM market_health_snapshot
                    WHERE session_date = ?
                    ORDER BY exchange, symbol_token
                    """,
                    (session_text,),
                ).fetchall()
                reference_rows = self._connection.execute(
                    """
                    SELECT * FROM market_session_reference
                    WHERE session_date = ?
                    ORDER BY reference_kind, exchange, symbol_token
                    """,
                    (session_text,),
                ).fetchall()
                candle_rows = self._connection.execute(
                    """
                    SELECT * FROM market_candles
                    WHERE start_at >= ? AND start_at < ?
                    ORDER BY exchange, symbol_token, start_at, interval_minutes
                    """,
                    (start_at, end_at),
                ).fetchall()
                tick_row = self._connection.execute(
                    """
                    SELECT COALESCE(MAX(id), 0) AS max_id
                    FROM market_ticks
                    WHERE exchange_timestamp >= ? AND exchange_timestamp < ?
                    """,
                    (start_at, end_at),
                ).fetchone()
                event_row = self._connection.execute(
                    """
                    SELECT COALESCE(MAX(id), 0) AS max_id
                    FROM market_feed_events
                    WHERE occurred_at >= ? AND occurred_at < ?
                    """,
                    (start_at, end_at),
                ).fetchone()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

        if checkpoint_row is None:
            raise ProjectionUnavailableError("projection checkpoint is missing")
        try:
            checkpoint = _row_to_projection_checkpoint(checkpoint_row)
            health = tuple(_row_to_instrument_health(row) for row in health_rows)
            references = tuple(
                (str(row["reference_kind"]), _row_to_session_reference(row))
                for row in reference_rows
            )
            candles = tuple(_row_to_candle(row) for row in candle_rows)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionStateCorruptError(
                "materialized projection snapshot is corrupt"
            ) from exc
        if checkpoint.session_date != session_date:
            raise ProjectionStateCorruptError(
                "projection checkpoint session date is inconsistent"
            )
        if evaluation is not None and any(
            timestamp is not None and timestamp > evaluation
            for timestamp in (
                checkpoint.updated_at,
                checkpoint.last_received_at,
                checkpoint.last_feed_event_at,
            )
        ):
            raise ProjectionCutoffBehindCheckpointError(
                "projection checkpoint is ahead of the requested read time"
            )
        raw_tick_id = int(tick_row["max_id"])
        raw_event_id = int(event_row["max_id"])
        if (
            checkpoint.last_tick_id > raw_tick_id
            or checkpoint.last_feed_event_id > raw_event_id
        ):
            raise ProjectionCheckpointAheadError(
                "projection checkpoint is ahead of authoritative raw storage"
            )
        if (
            checkpoint.last_tick_id < raw_tick_id
            or checkpoint.last_feed_event_id < raw_event_id
        ):
            raise ProjectionCatchUpPendingError(
                checkpoint_tick_id=checkpoint.last_tick_id,
                raw_tick_id=raw_tick_id,
                checkpoint_feed_event_id=checkpoint.last_feed_event_id,
                raw_feed_event_id=raw_event_id,
            )
        if any(item.feed_epoch > checkpoint.feed_epoch for item in health):
            raise ProjectionStateCorruptError(
                "projection health is ahead of the checkpoint feed epoch"
            )

        latest = tuple(
            item for kind, item in references if kind == "LATEST"
        )
        opening = tuple(
            item
            for kind, item in references
            if kind == "BEFORE_OPENING_RANGE"
        )
        projection_age = None
        if checkpoint.last_received_at is not None:
            age_at = checkpoint.updated_at if evaluation is None else evaluation
            projection_age = max(
                0.0,
                (age_at - checkpoint.last_received_at).total_seconds(),
            )
        return ProjectionView(
            checkpoint=checkpoint,
            candles=candles,
            health=health,
            session_references=latest,
            opening_range_references=opening_references_visible(
                {
                    (item.exchange, item.symbol_token): item
                    for item in opening
                },
                evaluation_at=checkpoint.updated_at,
            ),
            projection_age_seconds=projection_age,
            incremental_ticks_processed=0,
            rebuilt=False,
        )

    def save_projection_state(
        self,
        *,
        expected_checkpoint: ProjectionCheckpoint | None,
        checkpoint: ProjectionCheckpoint,
        health: Sequence[InstrumentHealth],
        latest_references: Sequence[SessionReference],
        first_five_references: Sequence[SessionReference],
        opening_range_references: Sequence[SessionReference],
        candles: Sequence[Candle],
    ) -> None:
        """Atomically replace materialized snapshots and advance the checkpoint."""
        session_text = checkpoint.session_date.isoformat()
        reference_groups = (
            ("LATEST", latest_references),
            ("BEFORE_FIRST_FIVE", first_five_references),
            ("BEFORE_OPENING_RANGE", opening_range_references),
        )
        if expected_checkpoint is not None and (
            checkpoint.last_tick_id < expected_checkpoint.last_tick_id
            or checkpoint.last_feed_event_id
            < expected_checkpoint.last_feed_event_id
            or checkpoint.updated_at < expected_checkpoint.updated_at
        ):
            raise ProjectionStateCorruptError(
                "projection checkpoint cannot move backward from its predecessor"
            )
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    """
                    SELECT * FROM market_projection_checkpoint
                    WHERE session_date = ?
                    """,
                    (session_text,),
                ).fetchone()
                if not _checkpoint_matches(existing, expected_checkpoint):
                    raise ProjectionConcurrentAdvanceError(
                        "projection changed after the writer restored its predecessor"
                    )
                for candle in candles:
                    self._connection.execute(_INSERT_CANDLE, _candle_values(candle))
                self._connection.execute(
                    "DELETE FROM market_health_snapshot WHERE session_date = ?",
                    (session_text,),
                )
                self._connection.executemany(
                    """
                    INSERT INTO market_health_snapshot(
                        session_date, exchange, symbol_token, feed_epoch, status,
                        latest_exchange_timestamp, latest_received_timestamp,
                        sequence_number, data_age_seconds, duplicate_count,
                        out_of_order_count, sequence_jump_count,
                        last_health_transition
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        _health_values(checkpoint.session_date, item)
                        for item in health
                    ],
                )
                self._connection.execute(
                    "DELETE FROM market_session_reference WHERE session_date = ?",
                    (session_text,),
                )
                reference_rows = [
                    _session_reference_values(kind, item)
                    for kind, references in reference_groups
                    for item in references
                ]
                if reference_rows:
                    self._connection.executemany(
                        """
                        INSERT INTO market_session_reference(
                            session_date, reference_kind, exchange, symbol_token,
                            trading_symbol, observed_at, session_open, session_high,
                            session_low, last_traded_price, average_traded_price,
                            cumulative_volume, provenance, trusted
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        reference_rows,
                    )
                self._connection.execute(
                    """
                    INSERT INTO market_projection_checkpoint(
                        session_date, last_tick_id, last_feed_event_id,
                        last_received_at, last_feed_event_at, feed_epoch, updated_at,
                        state_version, state_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_date) DO UPDATE SET
                        last_tick_id = excluded.last_tick_id,
                        last_feed_event_id = excluded.last_feed_event_id,
                        last_received_at = excluded.last_received_at,
                        last_feed_event_at = excluded.last_feed_event_at,
                        feed_epoch = excluded.feed_epoch,
                        updated_at = excluded.updated_at,
                        state_version = excluded.state_version,
                        state_json = excluded.state_json
                    """,
                    (
                        session_text,
                        checkpoint.last_tick_id,
                        checkpoint.last_feed_event_id,
                        _optional_datetime_iso(checkpoint.last_received_at),
                        _optional_datetime_iso(checkpoint.last_feed_event_at),
                        checkpoint.feed_epoch,
                        checkpoint.updated_at.isoformat(),
                        checkpoint.state_version,
                        _json(checkpoint.engine_state),
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def clear_projection_session(self, session_date: date) -> None:
        """Discard derived state while preserving authoritative raw rows."""
        start_at, end_at = _session_iso_bounds(session_date)
        session_text = session_date.isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM market_health_snapshot WHERE session_date = ?",
                (session_text,),
            )
            self._connection.execute(
                "DELETE FROM market_session_reference WHERE session_date = ?",
                (session_text,),
            )
            self._connection.execute(
                "DELETE FROM market_projection_checkpoint WHERE session_date = ?",
                (session_text,),
            )
            self._connection.execute(
                """
                DELETE FROM market_candles
                WHERE start_at >= ? AND start_at < ?
                  AND provenance IN ('LOCAL_TICKS', 'ANGEL_SESSION_FIELDS')
                """,
                (start_at, end_at),
            )

    def count_ticks(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        received_at_or_before: datetime | None = None,
    ) -> int:
        clauses: list[str] = []
        parameters: list[object] = []
        if start_at is not None:
            clauses.append("exchange_timestamp >= ?")
            parameters.append(_aware_iso(start_at))
        if end_at is not None:
            clauses.append("exchange_timestamp < ?")
            parameters.append(_aware_iso(end_at))
        if received_at_or_before is not None:
            clauses.append("received_at <= ?")
            parameters.append(_aware_iso(received_at_or_before))
        sql = "SELECT COUNT(*) AS count FROM market_ticks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            row = self._connection.execute(sql, parameters).fetchone()
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

_INSERT_CANDLE = """
INSERT INTO market_candles(
    exchange, symbol_token, trading_symbol, interval_minutes,
    start_at, end_at, open_price, high_price, low_price, close_price,
    volume, tick_count, completed, trusted, provenance
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(exchange, symbol_token, interval_minutes, start_at) DO UPDATE SET
    trading_symbol = excluded.trading_symbol,
    end_at = excluded.end_at,
    open_price = excluded.open_price,
    high_price = excluded.high_price,
    low_price = excluded.low_price,
    close_price = excluded.close_price,
    volume = excluded.volume,
    tick_count = excluded.tick_count,
    completed = excluded.completed,
    trusted = excluded.trusted,
    provenance = excluded.provenance
WHERE market_candles.trading_symbol IS NOT excluded.trading_symbol
   OR market_candles.end_at IS NOT excluded.end_at
   OR market_candles.open_price IS NOT excluded.open_price
   OR market_candles.high_price IS NOT excluded.high_price
   OR market_candles.low_price IS NOT excluded.low_price
   OR market_candles.close_price IS NOT excluded.close_price
   OR market_candles.volume IS NOT excluded.volume
   OR market_candles.tick_count IS NOT excluded.tick_count
   OR market_candles.completed IS NOT excluded.completed
   OR market_candles.trusted IS NOT excluded.trusted
   OR market_candles.provenance IS NOT excluded.provenance
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


def _candle_values(candle: Candle) -> tuple[object, ...]:
    if not candle.completed:
        raise ValueError("only completed candles may be persisted")
    return (
        candle.exchange.value,
        candle.symbol_token,
        candle.trading_symbol,
        int(candle.interval),
        candle.start_at.isoformat(),
        candle.end_at.isoformat(),
        str(candle.open_price),
        str(candle.high_price),
        str(candle.low_price),
        str(candle.close_price),
        candle.volume,
        candle.tick_count,
        int(candle.completed),
        int(candle.trusted),
        candle.provenance.value,
    )


def _row_to_candle(row: sqlite3.Row) -> Candle:
    return Candle(
        exchange=Exchange(row["exchange"]),
        symbol_token=row["symbol_token"],
        trading_symbol=row["trading_symbol"],
        interval=CandleInterval(row["interval_minutes"]),
        start_at=datetime.fromisoformat(row["start_at"]),
        end_at=datetime.fromisoformat(row["end_at"]),
        open_price=Decimal(row["open_price"]),
        high_price=Decimal(row["high_price"]),
        low_price=Decimal(row["low_price"]),
        close_price=Decimal(row["close_price"]),
        volume=row["volume"],
        tick_count=row["tick_count"],
        completed=bool(row["completed"]),
        trusted=bool(row["trusted"]),
        provenance=MarketDataProvenance(row["provenance"]),
    )


def _row_to_projection_checkpoint(row: sqlite3.Row) -> ProjectionCheckpoint:
    engine_state = json.loads(row["state_json"])
    if not isinstance(engine_state, dict):
        raise ValueError("projection state_json must contain an object")
    return ProjectionCheckpoint(
        session_date=date.fromisoformat(row["session_date"]),
        last_tick_id=int(row["last_tick_id"]),
        last_feed_event_id=int(row["last_feed_event_id"]),
        last_received_at=_optional_datetime(row["last_received_at"]),
        last_feed_event_at=_optional_datetime(row["last_feed_event_at"]),
        feed_epoch=int(row["feed_epoch"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        state_version=int(row["state_version"]),
        engine_state=engine_state,
    )


def _checkpoint_matches(
    row: sqlite3.Row | None,
    expected: ProjectionCheckpoint | None,
) -> bool:
    if row is None:
        return expected is None
    if expected is None:
        return False
    return (
        row["session_date"] == expected.session_date.isoformat()
        and int(row["last_tick_id"]) == expected.last_tick_id
        and int(row["last_feed_event_id"]) == expected.last_feed_event_id
        and row["last_received_at"]
        == _optional_datetime_iso(expected.last_received_at)
        and row["last_feed_event_at"]
        == _optional_datetime_iso(expected.last_feed_event_at)
        and int(row["feed_epoch"]) == expected.feed_epoch
        and row["updated_at"] == expected.updated_at.isoformat()
        and int(row["state_version"]) == expected.state_version
        and row["state_json"] == _json(expected.engine_state)
    )


def _row_to_instrument_health(row: sqlite3.Row) -> InstrumentHealth:
    return InstrumentHealth(
        exchange=Exchange(row["exchange"]),
        symbol_token=row["symbol_token"],
        feed_epoch=int(row["feed_epoch"]),
        status=MarketDataHealthStatus(row["status"]),
        latest_exchange_timestamp=_optional_datetime(
            row["latest_exchange_timestamp"]
        ),
        latest_received_timestamp=_optional_datetime(
            row["latest_received_timestamp"]
        ),
        sequence_number=(
            None
            if row["sequence_number"] is None
            else int(row["sequence_number"])
        ),
        data_age_seconds=(
            None
            if row["data_age_seconds"] is None
            else float(row["data_age_seconds"])
        ),
        duplicate_count=int(row["duplicate_count"]),
        out_of_order_count=int(row["out_of_order_count"]),
        sequence_jump_count=int(row["sequence_jump_count"]),
        last_health_transition=datetime.fromisoformat(
            row["last_health_transition"]
        ),
    )


def _health_values(
    session_date: date, health: InstrumentHealth
) -> tuple[object, ...]:
    return (
        session_date.isoformat(),
        health.exchange.value,
        health.symbol_token,
        health.feed_epoch,
        health.status.value,
        _optional_datetime_iso(health.latest_exchange_timestamp),
        _optional_datetime_iso(health.latest_received_timestamp),
        health.sequence_number,
        health.data_age_seconds,
        health.duplicate_count,
        health.out_of_order_count,
        health.sequence_jump_count,
        health.last_health_transition.isoformat(),
    )


def _session_reference_values(
    reference_kind: str, reference: SessionReference
) -> tuple[object, ...]:
    return (
        reference.session_date.isoformat(),
        reference_kind,
        reference.exchange.value,
        reference.symbol_token,
        reference.trading_symbol,
        reference.observed_at.isoformat(),
        str(reference.session_open),
        str(reference.session_high),
        str(reference.session_low),
        str(reference.last_traded_price),
        _optional_decimal_string(reference.average_traded_price),
        reference.cumulative_volume,
        reference.provenance.value,
        int(reference.trusted),
    )


def _row_to_session_reference(row: sqlite3.Row) -> SessionReference:
    return SessionReference(
        exchange=Exchange(row["exchange"]),
        symbol_token=row["symbol_token"],
        trading_symbol=row["trading_symbol"],
        session_date=date.fromisoformat(row["session_date"]),
        observed_at=datetime.fromisoformat(row["observed_at"]),
        session_open=Decimal(row["session_open"]),
        session_high=Decimal(row["session_high"]),
        session_low=Decimal(row["session_low"]),
        last_traded_price=Decimal(row["last_traded_price"]),
        average_traded_price=_optional_decimal(row["average_traded_price"]),
        cumulative_volume=row["cumulative_volume"],
        provenance=MarketDataProvenance(row["provenance"]),
        trusted=bool(row["trusted"]),
    )


def _ensure_column(
    connection: sqlite3.Connection,
    *,
    table: str,
    column: str,
    definition: str,
) -> None:
    existing = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _optional_decimal_string(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _optional_datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _optional_datetime_iso(value: datetime | None) -> str | None:
    return None if value is None else _aware_iso(value)


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp filters must be timezone-aware")
    return value.astimezone(IST).isoformat()


def _session_iso_bounds(session_date: date) -> tuple[str, str]:
    start = datetime.combine(session_date, time.min, tzinfo=IST)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()
