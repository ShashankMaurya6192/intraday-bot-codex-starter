"""SQLite persistence for deterministic paper execution and audit state."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from trading_bot.domain import IST, Exchange
from trading_bot.paper.models import (
    PaperAuditEvent,
    PaperFill,
    PaperOrder,
    PaperOrderPurpose,
    PaperOrderStatus,
    PaperPosition,
    PaperPositionStatus,
    PaperTradingDay,
)
from trading_bot.risk import TradeSide

from .sqlite import sqlite_path_from_url

_PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trading_day (
    session_date TEXT PRIMARY KEY,
    starting_equity TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    capital_reserved TEXT NOT NULL,
    trade_count INTEGER NOT NULL CHECK (trade_count >= 0),
    consecutive_losses INTEGER NOT NULL CHECK (consecutive_losses >= 0),
    kill_switch_active INTEGER NOT NULL CHECK (kill_switch_active IN (0, 1)),
    state_uncertain INTEGER NOT NULL CHECK (state_uncertain IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    client_tag TEXT NOT NULL UNIQUE,
    session_date TEXT NOT NULL,
    exchange TEXT NOT NULL,
    symbol_token TEXT NOT NULL,
    trading_symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    purpose TEXT NOT NULL,
    requested_quantity INTEGER NOT NULL CHECK (requested_quantity >= 0),
    filled_quantity INTEGER NOT NULL CHECK (
        filled_quantity >= 0 AND filled_quantity <= requested_quantity
    ),
    reference_price TEXT NOT NULL,
    protective_stop TEXT,
    planned_risk_per_share TEXT,
    tick_size TEXT NOT NULL,
    lot_size INTEGER NOT NULL CHECK (lot_size > 0),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    rejection_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_paper_orders_session_status
    ON paper_orders(session_date, status, created_at);

CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    session_date TEXT NOT NULL,
    exchange TEXT NOT NULL,
    symbol_token TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    simulation_model TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES paper_orders(order_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_fills_order
    ON paper_fills(order_id, filled_at, fill_id);

CREATE TABLE IF NOT EXISTS paper_positions (
    session_date TEXT NOT NULL,
    exchange TEXT NOT NULL,
    symbol_token TEXT NOT NULL,
    trading_symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_order_id TEXT NOT NULL,
    initial_quantity INTEGER NOT NULL CHECK (initial_quantity > 0),
    remaining_quantity INTEGER NOT NULL CHECK (remaining_quantity >= 0),
    lot_size INTEGER NOT NULL CHECK (lot_size > 0),
    average_entry_price TEXT NOT NULL,
    last_mark_price TEXT NOT NULL,
    last_mark_at TEXT NOT NULL,
    protective_stop TEXT NOT NULL,
    planned_risk_per_share TEXT NOT NULL,
    tick_size TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    first_partial_started INTEGER NOT NULL CHECK (first_partial_started IN (0, 1)),
    second_partial_started INTEGER NOT NULL CHECK (second_partial_started IN (0, 1)),
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(session_date, exchange, symbol_token),
    FOREIGN KEY(entry_order_id) REFERENCES paper_orders(order_id)
);

CREATE TABLE IF NOT EXISTS paper_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_id TEXT,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_audit_session
    ON paper_audit_events(session_date, occurred_at, id);
"""


class SQLitePaperStore:
    """Persist paper state atomically without sharing any broker-order surface."""

    def __init__(self, database_url: str, *, busy_timeout_ms: int = 5_000) -> None:
        path = sqlite_path_from_url(database_url)
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            path,
            timeout=busy_timeout_ms / 1_000,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.executescript(_PAPER_SCHEMA)
            self._connection.commit()

    def initialize_day(
        self, session_date: date, *, starting_equity: Decimal, now: datetime
    ) -> PaperTradingDay:
        proposed = PaperTradingDay(
            session_date=session_date,
            starting_equity=starting_equity,
            updated_at=now,
        )
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT * FROM paper_trading_day WHERE session_date = ?",
                    (session_date.isoformat(),),
                ).fetchone()
                if row is None:
                    self._upsert_day(proposed)
                    day = proposed
                    self._append_audit(
                        session_date=session_date,
                        occurred_at=now,
                        event_type="DAY_INITIALIZED",
                        entity_id=None,
                        details={"starting_equity": str(proposed.starting_equity)},
                    )
                else:
                    day = _row_to_day(row)
                    if day.starting_equity != proposed.starting_equity:
                        raise ValueError(
                            "paper day already exists with different starting equity"
                        )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return day

    def load_day(self, session_date: date) -> PaperTradingDay | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM paper_trading_day WHERE session_date = ?",
                (session_date.isoformat(),),
            ).fetchone()
        return None if row is None else _row_to_day(row)

    def load_order_by_client_tag(self, client_tag: str) -> PaperOrder | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM paper_orders WHERE client_tag = ?",
                (client_tag,),
            ).fetchone()
        return None if row is None else _row_to_order(row)

    def has_fill(self, fill_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM paper_fills WHERE fill_id = ?",
                (fill_id,),
            ).fetchone()
        return row is not None

    def list_orders(
        self,
        session_date: date,
        *,
        symbol_token: str | None = None,
    ) -> tuple[PaperOrder, ...]:
        sql = "SELECT * FROM paper_orders WHERE session_date = ?"
        parameters: list[object] = [session_date.isoformat()]
        if symbol_token is not None:
            sql += " AND symbol_token = ?"
            parameters.append(symbol_token)
        sql += " ORDER BY created_at, order_id"
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return tuple(_row_to_order(row) for row in rows)

    def list_fills(
        self, session_date: date, *, order_id: str | None = None
    ) -> tuple[PaperFill, ...]:
        sql = "SELECT * FROM paper_fills WHERE session_date = ?"
        parameters: list[object] = [session_date.isoformat()]
        if order_id is not None:
            sql += " AND order_id = ?"
            parameters.append(order_id)
        sql += " ORDER BY filled_at, fill_id"
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return tuple(_row_to_fill(row) for row in rows)

    def list_positions(
        self, session_date: date, *, include_closed: bool = True
    ) -> tuple[PaperPosition, ...]:
        sql = "SELECT * FROM paper_positions WHERE session_date = ?"
        parameters: list[object] = [session_date.isoformat()]
        if not include_closed:
            sql += " AND status = 'OPEN'"
        sql += " ORDER BY exchange, symbol_token"
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return tuple(_row_to_position(row) for row in rows)

    def list_audit_events(self, session_date: date) -> tuple[PaperAuditEvent, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM paper_audit_events
                WHERE session_date = ? ORDER BY occurred_at, id
                """,
                (session_date.isoformat(),),
            ).fetchall()
        return tuple(_row_to_audit(row) for row in rows)

    def save_transition(
        self,
        *,
        day: PaperTradingDay,
        orders: Sequence[PaperOrder] = (),
        positions: Sequence[PaperPosition] = (),
        fills: Sequence[PaperFill] = (),
        event_type: str,
        occurred_at: datetime,
        entity_id: str | None,
        details: dict[str, object],
    ) -> None:
        if any(item.session_date != day.session_date for item in orders):
            raise ValueError("paper order transition crossed session boundary")
        if any(item.session_date != day.session_date for item in positions):
            raise ValueError("paper position transition crossed session boundary")
        if any(item.session_date != day.session_date for item in fills):
            raise ValueError("paper fill transition crossed session boundary")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if (
                    self._connection.execute(
                        "SELECT 1 FROM paper_trading_day WHERE session_date = ?",
                        (day.session_date.isoformat(),),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("paper trading day is not initialized")
                self._upsert_day(day)
                for order in orders:
                    self._upsert_order(order)
                for position in positions:
                    self._upsert_position(position)
                for fill in fills:
                    self._insert_fill(fill)
                self._append_audit(
                    session_date=day.session_date,
                    occurred_at=occurred_at,
                    event_type=event_type,
                    entity_id=entity_id,
                    details=details,
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLitePaperStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _upsert_day(self, day: PaperTradingDay) -> None:
        self._connection.execute(
            """
            INSERT INTO paper_trading_day(
                session_date, starting_equity, realized_pnl, capital_reserved,
                trade_count, consecutive_losses, kill_switch_active,
                state_uncertain, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_date) DO UPDATE SET
                starting_equity = excluded.starting_equity,
                realized_pnl = excluded.realized_pnl,
                capital_reserved = excluded.capital_reserved,
                trade_count = excluded.trade_count,
                consecutive_losses = excluded.consecutive_losses,
                kill_switch_active = excluded.kill_switch_active,
                state_uncertain = excluded.state_uncertain,
                updated_at = excluded.updated_at
            """,
            (
                day.session_date.isoformat(),
                str(day.starting_equity),
                str(day.realized_pnl),
                str(day.capital_reserved),
                day.trade_count,
                day.consecutive_losses,
                int(day.kill_switch_active),
                int(day.state_uncertain),
                day.updated_at.isoformat(),
            ),
        )

    def _upsert_order(self, order: PaperOrder) -> None:
        self._connection.execute(
            """
            INSERT INTO paper_orders VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(order_id) DO UPDATE SET
                filled_quantity = excluded.filled_quantity,
                status = excluded.status,
                updated_at = excluded.updated_at,
                rejection_reason = excluded.rejection_reason
            """,
            _order_values(order),
        )

    def _upsert_position(self, position: PaperPosition) -> None:
        self._connection.execute(
            """
            INSERT INTO paper_positions VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(session_date, exchange, symbol_token) DO UPDATE SET
                initial_quantity = excluded.initial_quantity,
                remaining_quantity = excluded.remaining_quantity,
                lot_size = excluded.lot_size,
                average_entry_price = excluded.average_entry_price,
                last_mark_price = excluded.last_mark_price,
                last_mark_at = excluded.last_mark_at,
                protective_stop = excluded.protective_stop,
                realized_pnl = excluded.realized_pnl,
                first_partial_started = excluded.first_partial_started,
                second_partial_started = excluded.second_partial_started,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            _position_values(position),
        )

    def _insert_fill(self, fill: PaperFill) -> None:
        self._connection.execute(
            "INSERT INTO paper_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fill.fill_id,
                fill.order_id,
                fill.session_date.isoformat(),
                fill.exchange.value,
                fill.symbol_token,
                fill.side.value,
                fill.quantity,
                str(fill.price),
                fill.filled_at.isoformat(),
                fill.simulation_model,
            ),
        )

    def _append_audit(
        self,
        *,
        session_date: date,
        occurred_at: datetime,
        event_type: str,
        entity_id: str | None,
        details: dict[str, object],
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO paper_audit_events(
                session_date, occurred_at, event_type, entity_id, details_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_date.isoformat(),
                occurred_at.astimezone(IST).isoformat(),
                event_type,
                entity_id,
                json.dumps(details, sort_keys=True, separators=(",", ":")),
            ),
        )


def _order_values(order: PaperOrder) -> tuple[object, ...]:
    return (
        order.order_id,
        order.client_tag,
        order.session_date.isoformat(),
        order.exchange.value,
        order.symbol_token,
        order.trading_symbol,
        order.side.value,
        order.purpose.value,
        order.requested_quantity,
        order.filled_quantity,
        str(order.reference_price),
        None if order.protective_stop is None else str(order.protective_stop),
        (
            None
            if order.planned_risk_per_share is None
            else str(order.planned_risk_per_share)
        ),
        str(order.tick_size),
        order.lot_size,
        order.status.value,
        order.created_at.isoformat(),
        order.updated_at.isoformat(),
        order.rejection_reason,
    )


def _position_values(position: PaperPosition) -> tuple[object, ...]:
    return (
        position.session_date.isoformat(),
        position.exchange.value,
        position.symbol_token,
        position.trading_symbol,
        position.side.value,
        position.entry_order_id,
        position.initial_quantity,
        position.remaining_quantity,
        position.lot_size,
        str(position.average_entry_price),
        str(position.last_mark_price),
        position.last_mark_at.isoformat(),
        str(position.protective_stop),
        str(position.planned_risk_per_share),
        str(position.tick_size),
        str(position.realized_pnl),
        int(position.first_partial_started),
        int(position.second_partial_started),
        position.status.value,
        position.opened_at.isoformat(),
        position.updated_at.isoformat(),
    )


def _row_to_day(row: sqlite3.Row) -> PaperTradingDay:
    return PaperTradingDay(
        session_date=date.fromisoformat(row["session_date"]),
        starting_equity=Decimal(row["starting_equity"]),
        realized_pnl=Decimal(row["realized_pnl"]),
        capital_reserved=Decimal(row["capital_reserved"]),
        trade_count=int(row["trade_count"]),
        consecutive_losses=int(row["consecutive_losses"]),
        kill_switch_active=bool(row["kill_switch_active"]),
        state_uncertain=bool(row["state_uncertain"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_order(row: sqlite3.Row) -> PaperOrder:
    return PaperOrder(
        order_id=row["order_id"],
        client_tag=row["client_tag"],
        session_date=date.fromisoformat(row["session_date"]),
        exchange=Exchange(row["exchange"]),
        symbol_token=row["symbol_token"],
        trading_symbol=row["trading_symbol"],
        side=TradeSide(row["side"]),
        purpose=PaperOrderPurpose(row["purpose"]),
        requested_quantity=int(row["requested_quantity"]),
        filled_quantity=int(row["filled_quantity"]),
        reference_price=Decimal(row["reference_price"]),
        protective_stop=_optional_decimal(row["protective_stop"]),
        planned_risk_per_share=_optional_decimal(row["planned_risk_per_share"]),
        tick_size=Decimal(row["tick_size"]),
        lot_size=int(row["lot_size"]),
        status=PaperOrderStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        rejection_reason=row["rejection_reason"],
    )


def _row_to_fill(row: sqlite3.Row) -> PaperFill:
    return PaperFill(
        fill_id=row["fill_id"],
        order_id=row["order_id"],
        session_date=date.fromisoformat(row["session_date"]),
        exchange=Exchange(row["exchange"]),
        symbol_token=row["symbol_token"],
        side=TradeSide(row["side"]),
        quantity=int(row["quantity"]),
        price=Decimal(row["price"]),
        filled_at=datetime.fromisoformat(row["filled_at"]),
        simulation_model=row["simulation_model"],
    )


def _row_to_position(row: sqlite3.Row) -> PaperPosition:
    return PaperPosition(
        session_date=date.fromisoformat(row["session_date"]),
        exchange=Exchange(row["exchange"]),
        symbol_token=row["symbol_token"],
        trading_symbol=row["trading_symbol"],
        side=TradeSide(row["side"]),
        entry_order_id=row["entry_order_id"],
        initial_quantity=int(row["initial_quantity"]),
        remaining_quantity=int(row["remaining_quantity"]),
        lot_size=int(row["lot_size"]),
        average_entry_price=Decimal(row["average_entry_price"]),
        last_mark_price=Decimal(row["last_mark_price"]),
        last_mark_at=datetime.fromisoformat(row["last_mark_at"]),
        protective_stop=Decimal(row["protective_stop"]),
        planned_risk_per_share=Decimal(row["planned_risk_per_share"]),
        tick_size=Decimal(row["tick_size"]),
        realized_pnl=Decimal(row["realized_pnl"]),
        first_partial_started=bool(row["first_partial_started"]),
        second_partial_started=bool(row["second_partial_started"]),
        status=PaperPositionStatus(row["status"]),
        opened_at=datetime.fromisoformat(row["opened_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_audit(row: sqlite3.Row) -> PaperAuditEvent:
    details = json.loads(row["details_json"])
    if not isinstance(details, dict):
        raise ValueError("paper audit details must be an object")
    return PaperAuditEvent(
        event_id=int(row["id"]),
        session_date=date.fromisoformat(row["session_date"]),
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        event_type=row["event_type"],
        entity_id=row["entity_id"],
        details=details,
    )


def _optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)
