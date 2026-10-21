from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain import IST, Exchange, MarketTick, SubscriptionMode
from trading_bot.marketdata import MarketDataHealthStatus
from trading_bot.paper import (
    PaperOrderPurpose,
    PaperOrderStatus,
    PaperPositionStatus,
    PaperTradeIntent,
)
from trading_bot.paper.execution import PaperExecutionConfig, PaperExecutionEngine
from trading_bot.risk import RiskLimits, TradeSide
from trading_bot.storage import SQLitePaperStore

SESSION = date(2026, 10, 21)
ENTRY_AT = datetime(2026, 10, 21, 10, 0, tzinfo=IST)


def _limits(**changes: object) -> RiskLimits:
    values: dict[str, object] = {
        "risk_per_trade_pct": Decimal("0.003"),
        "max_daily_loss_pct": Decimal("0.009"),
        "max_open_positions": 2,
        "max_trades_per_day": 4,
        "max_consecutive_losses": 3,
        "max_capital_utilization_pct": Decimal("1"),
    }
    values.update(changes)
    return RiskLimits(**values)  # type: ignore[arg-type]


def _config(**changes: object) -> PaperExecutionConfig:
    values: dict[str, object] = {
        "slippage_bps": Decimal(0),
        "tick_participation_pct": Decimal(1),
        "first_partial_pct": Decimal("0.50"),
        "second_partial_pct": Decimal("0.25"),
        "stale_after_seconds": 5.0,
    }
    values.update(changes)
    return PaperExecutionConfig(**values)  # type: ignore[arg-type]


def _engine(
    tmp_path,
    *,
    config: PaperExecutionConfig | None = None,
    limits: RiskLimits | None = None,
) -> tuple[SQLitePaperStore, PaperExecutionEngine]:
    store = SQLitePaperStore(f"sqlite:///{(tmp_path / 'paper.db').as_posix()}")
    engine = PaperExecutionEngine(
        store,
        limits or _limits(),
        config=config or _config(),
    )
    engine.initialize_day(
        SESSION,
        starting_equity=Decimal("100000"),
        now=ENTRY_AT,
    )
    return store, engine


def _intent(
    *,
    client_tag: str = "signal-1",
    side: TradeSide = TradeSide.LONG,
    entry: str = "100",
    stop: str = "95",
    submitted_at: datetime = ENTRY_AT,
    token: str = "3045",
) -> PaperTradeIntent:
    return PaperTradeIntent(
        client_tag=client_tag,
        session_date=SESSION,
        exchange=Exchange.NSE_CM,
        symbol_token=token,
        trading_symbol="SBIN-EQ",
        side=side,
        reference_entry_price=Decimal(entry),
        protective_stop=Decimal(stop),
        tick_size=Decimal("0.05"),
        lot_size=1,
        max_liquidity_quantity=1_000,
        submitted_at=submitted_at,
        signal_qualified=True,
        market_data_trusted=True,
    )


def _tick(
    at: datetime,
    *,
    price: str = "100",
    quantity: int | None = 1_000,
    sequence: int = 1,
    token: str = "3045",
) -> MarketTick:
    return MarketTick(
        exchange=Exchange.NSE_CM,
        symbol_token=token,
        trading_symbol="SBIN-EQ",
        subscription_mode=SubscriptionMode.QUOTE,
        sequence_number=sequence,
        exchange_timestamp=at,
        received_at=at,
        last_traded_price=Decimal(price),
        last_traded_quantity=quantity,
    )


def _ready_tick(
    engine: PaperExecutionEngine,
    tick: MarketTick,
    *,
    observed_at: datetime | None = None,
) -> tuple:
    return engine.on_tick(
        tick,
        health_status=MarketDataHealthStatus.READY,
        observed_at=observed_at or tick.received_at,
    )


def test_entry_is_idempotent_and_never_fills_on_submission_tick(tmp_path) -> None:
    store, engine = _engine(tmp_path)
    result = engine.submit_entry(_intent())

    assert result.accepted
    assert result.order.requested_quantity == 60
    assert _ready_tick(engine, _tick(ENTRY_AT)) == ()

    fills = _ready_tick(engine, _tick(ENTRY_AT + timedelta(seconds=1), sequence=2))
    assert len(fills) == 1
    assert fills[0].quantity == 60
    assert fills[0].price == Decimal("100")

    replay = engine.submit_entry(_intent())
    assert replay.idempotent_replay
    assert replay.order.order_id == result.order.order_id
    assert len(store.list_orders(SESSION)) == 1
    assert len(store.list_fills(SESSION)) == 1
    position = store.list_positions(SESSION)[0]
    assert position.status is PaperPositionStatus.OPEN
    assert position.remaining_quantity == 60
    store.close()


def test_fill_is_limited_by_tick_participation_and_survives_restart(tmp_path) -> None:
    store, engine = _engine(
        tmp_path,
        config=_config(tick_participation_pct=Decimal("0.50")),
    )
    engine.submit_entry(_intent())
    first = _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=1), quantity=60, sequence=2),
    )
    assert first[0].quantity == 30
    assert store.list_orders(SESSION)[0].status is PaperOrderStatus.PARTIALLY_FILLED
    store.close()

    reopened = SQLitePaperStore(
        f"sqlite:///{(tmp_path / 'paper.db').as_posix()}"
    )
    restarted = PaperExecutionEngine(reopened, _limits(), config=_config(
        tick_participation_pct=Decimal("0.50")
    ))
    second = _ready_tick(
        restarted,
        _tick(ENTRY_AT + timedelta(seconds=2), quantity=60, sequence=3),
    )
    assert second[0].quantity == 30
    assert reopened.list_orders(SESSION)[0].status is PaperOrderStatus.FILLED
    assert reopened.list_positions(SESSION)[0].remaining_quantity == 60
    reopened.close()


def test_same_tick_replay_cannot_duplicate_a_fill(tmp_path) -> None:
    store, engine = _engine(
        tmp_path,
        config=_config(tick_participation_pct=Decimal("0.50")),
    )
    engine.submit_entry(_intent())
    tick = _tick(ENTRY_AT + timedelta(seconds=1), quantity=60, sequence=2)

    assert len(_ready_tick(engine, tick)) == 1
    assert _ready_tick(engine, tick) == ()
    assert len(store.list_fills(SESSION)) == 1
    assert store.list_positions(SESSION)[0].remaining_quantity == 30
    store.close()


def test_adverse_entry_price_drift_cancels_instead_of_increasing_risk(
    tmp_path,
) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())

    assert _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=1), price="101", sequence=2),
    ) == ()
    order = store.list_orders(SESSION)[0]
    assert order.status is PaperOrderStatus.CANCELLED
    assert order.rejection_reason == "entry_price_exceeds_risk_or_capital_plan"
    assert store.list_fills(SESSION) == ()
    assert store.list_positions(SESSION) == ()
    assert store.load_day(SESSION).capital_reserved == 0  # type: ignore[union-attr]
    store.close()


def test_adverse_drift_cancels_only_unfilled_remainder(tmp_path) -> None:
    store, engine = _engine(
        tmp_path,
        config=_config(tick_participation_pct=Decimal("0.50")),
    )
    engine.submit_entry(_intent())
    _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=1), quantity=60, sequence=2),
    )

    _ready_tick(
        engine,
        _tick(
            ENTRY_AT + timedelta(seconds=2),
            price="101",
            quantity=60,
            sequence=3,
        ),
    )
    order = store.list_orders(SESSION)[0]
    position = store.list_positions(SESSION)[0]
    assert order.status is PaperOrderStatus.CANCELLED
    assert order.filled_quantity == 30
    assert position.remaining_quantity == 30
    assert position.protective_stop == Decimal("95")
    assert store.load_day(SESSION).capital_reserved == Decimal("3000")  # type: ignore[union-attr]
    store.close()


def test_hard_stop_uses_next_tick_and_records_realized_loss(tmp_path) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())
    _ready_tick(engine, _tick(ENTRY_AT + timedelta(seconds=1), sequence=2))

    trigger = _tick(ENTRY_AT + timedelta(seconds=2), price="94", sequence=3)
    assert _ready_tick(engine, trigger) == ()
    stop_order = store.list_orders(SESSION)[-1]
    assert stop_order.purpose is PaperOrderPurpose.HARD_STOP
    assert stop_order.status is PaperOrderStatus.PENDING

    fills = _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=3), price="93", sequence=4),
    )
    assert fills[0].price == Decimal("93")
    position = store.list_positions(SESSION)[0]
    assert position.status is PaperPositionStatus.CLOSED
    assert position.realized_pnl == Decimal("-420")
    assert store.load_day(SESSION).realized_pnl == Decimal("-420")  # type: ignore[union-attr]
    store.close()


def test_partial_entry_is_cancelled_before_protective_exit(tmp_path) -> None:
    store, engine = _engine(
        tmp_path,
        config=_config(tick_participation_pct=Decimal("0.50")),
    )
    engine.submit_entry(_intent())
    _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=1), quantity=60, sequence=2),
    )

    _ready_tick(
        engine,
        _tick(
            ENTRY_AT + timedelta(seconds=2),
            price="94",
            quantity=60,
            sequence=3,
        ),
    )
    orders = store.list_orders(SESSION)
    assert orders[0].status is PaperOrderStatus.CANCELLED
    assert orders[0].filled_quantity == 30
    assert orders[1].purpose is PaperOrderPurpose.HARD_STOP
    assert orders[1].requested_quantity == 30

    _ready_tick(
        engine,
        _tick(
            ENTRY_AT + timedelta(seconds=3),
            price="93",
            quantity=60,
            sequence=4,
        ),
    )
    assert store.list_positions(SESSION)[0].status is PaperPositionStatus.CLOSED
    assert store.load_day(SESSION).capital_reserved == 0  # type: ignore[union-attr]
    store.close()


def test_short_position_uses_inverse_stop_and_pnl_direction(tmp_path) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(
        _intent(side=TradeSide.SHORT, entry="100", stop="105")
    )
    _ready_tick(engine, _tick(ENTRY_AT + timedelta(seconds=1), sequence=2))

    _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=2), price="106", sequence=3),
    )
    _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=3), price="107", sequence=4),
    )

    position = store.list_positions(SESSION)[0]
    assert position.status is PaperPositionStatus.CLOSED
    assert position.realized_pnl == Decimal("-420")
    store.close()


def test_partial_targets_are_triggered_then_filled_on_a_later_tick(tmp_path) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())
    _ready_tick(engine, _tick(ENTRY_AT + timedelta(seconds=1), sequence=2))

    assert _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=2), price="105", sequence=3),
    ) == ()
    target = store.list_orders(SESSION)[-1]
    assert target.purpose is PaperOrderPurpose.PARTIAL_ONE_R
    assert target.requested_quantity == 30

    _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=3), price="106", sequence=4),
    )
    position = store.list_positions(SESSION)[0]
    assert position.remaining_quantity == 30
    assert position.realized_pnl == Decimal("180")
    store.close()


def test_untrusted_tick_cancels_entries_and_requires_flat_reconciliation(
    tmp_path,
) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())
    tick = _tick(ENTRY_AT + timedelta(seconds=1), sequence=2)

    assert engine.on_tick(
        tick,
        health_status=MarketDataHealthStatus.DEGRADED,
        observed_at=tick.received_at,
    ) == ()
    day = store.load_day(SESSION)
    assert day is not None and day.state_uncertain
    assert store.list_orders(SESSION)[0].status is PaperOrderStatus.CANCELLED

    rejected = engine.submit_entry(_intent(client_tag="signal-2", token="1594"))
    assert not rejected.accepted
    assert "state_uncertain" in (rejected.order.rejection_reason or "")
    reconciled = engine.reconcile_flat_day(
        SESSION,
        now=ENTRY_AT + timedelta(seconds=2),
    )
    assert not reconciled.state_uncertain
    store.close()


def test_stale_tick_fails_closed(tmp_path) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())
    tick = _tick(ENTRY_AT + timedelta(seconds=1), sequence=2)

    assert _ready_tick(
        engine,
        tick,
        observed_at=tick.received_at + timedelta(seconds=6),
    ) == ()
    assert store.load_day(SESSION).state_uncertain  # type: ignore[union-attr]
    store.close()


def test_untrusted_tick_without_related_exposure_does_not_poison_day(
    tmp_path,
) -> None:
    store, engine = _engine(tmp_path)
    unrelated = _tick(
        ENTRY_AT + timedelta(seconds=1),
        token="999",
        sequence=2,
    )

    assert engine.on_tick(
        unrelated,
        health_status=MarketDataHealthStatus.DEGRADED,
        observed_at=unrelated.received_at,
    ) == ()
    assert not store.load_day(SESSION).state_uncertain  # type: ignore[union-attr]
    store.close()


def test_global_pipeline_failure_cancels_entries_and_marks_uncertain(
    tmp_path,
) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())

    engine.mark_state_uncertain(
        SESSION,
        now=ENTRY_AT + timedelta(seconds=1),
        reason="tick_writer_failure",
    )

    assert store.load_day(SESSION).state_uncertain  # type: ignore[union-attr]
    assert store.list_orders(SESSION)[0].status is PaperOrderStatus.CANCELLED
    store.close()


def test_stale_mark_on_an_existing_position_blocks_another_entry(
    tmp_path,
) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())
    _ready_tick(engine, _tick(ENTRY_AT + timedelta(seconds=1), sequence=2))

    result = engine.submit_entry(
        _intent(
            client_tag="signal-2",
            token="1594",
            submitted_at=ENTRY_AT + timedelta(seconds=7),
        )
    )

    assert not result.accepted
    assert "state_uncertain" in (result.order.rejection_reason or "")
    store.close()


def test_kill_switch_blocks_entries_and_queues_position_exit(tmp_path) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())
    _ready_tick(engine, _tick(ENTRY_AT + timedelta(seconds=1), sequence=2))

    killed = engine.activate_kill_switch(
        SESSION,
        now=ENTRY_AT + timedelta(seconds=2),
        reason="operator_test",
    )
    assert killed.kill_switch_active
    exit_order = store.list_orders(SESSION)[-1]
    assert exit_order.purpose is PaperOrderPurpose.KILL_SWITCH_EXIT

    rejected = engine.submit_entry(_intent(client_tag="signal-2", token="1594"))
    assert not rejected.accepted
    assert "kill_switch_active" in (rejected.order.rejection_reason or "")
    _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=3), price="99", sequence=3),
    )
    assert store.list_positions(SESSION)[0].status is PaperPositionStatus.CLOSED
    store.close()


def test_daily_loss_automatically_activates_kill_switch(tmp_path) -> None:
    store, engine = _engine(
        tmp_path,
        limits=_limits(max_daily_loss_pct=Decimal("0.004")),
    )
    engine.submit_entry(_intent())
    _ready_tick(engine, _tick(ENTRY_AT + timedelta(seconds=1), sequence=2))
    _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=2), price="90", sequence=3),
    )
    _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=3), price="90", sequence=4),
    )

    day = store.load_day(SESSION)
    assert day is not None
    assert day.realized_pnl == Decimal("-600")
    assert day.kill_switch_active
    store.close()


def test_repeated_kill_switch_preserves_a_partial_exit_order(tmp_path) -> None:
    store, engine = _engine(
        tmp_path,
        config=_config(tick_participation_pct=Decimal("0.50")),
    )
    engine.submit_entry(_intent())
    _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=1), quantity=120, sequence=2),
    )
    engine.activate_kill_switch(
        SESSION,
        now=ENTRY_AT + timedelta(seconds=2),
        reason="first",
    )
    _ready_tick(
        engine,
        _tick(
            ENTRY_AT + timedelta(seconds=3),
            price="99",
            quantity=60,
            sequence=3,
        ),
    )
    partial = store.list_orders(SESSION)[-1]
    assert partial.status is PaperOrderStatus.PARTIALLY_FILLED
    assert partial.filled_quantity == 30

    engine.activate_kill_switch(
        SESSION,
        now=ENTRY_AT + timedelta(seconds=4),
        reason="repeated",
    )
    preserved = store.list_orders(SESSION)[-1]
    assert preserved.order_id == partial.order_id
    assert preserved.requested_quantity == 60
    assert preserved.filled_quantity == 30

    _ready_tick(
        engine,
        _tick(
            ENTRY_AT + timedelta(seconds=5),
            price="99",
            quantity=60,
            sequence=4,
        ),
    )
    assert store.list_positions(SESSION)[0].status is PaperPositionStatus.CLOSED
    store.close()


def test_time_exit_is_queued_at_square_off_and_filled_next_tick(tmp_path) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())
    _ready_tick(engine, _tick(ENTRY_AT + timedelta(seconds=1), sequence=2))
    square_off = datetime(2026, 10, 21, 15, 15, tzinfo=IST)

    assert _ready_tick(engine, _tick(square_off, price="101", sequence=3)) == ()
    assert store.list_orders(SESSION)[-1].purpose is PaperOrderPurpose.TIME_EXIT
    _ready_tick(
        engine,
        _tick(square_off + timedelta(seconds=1), price="101", sequence=4),
    )
    assert store.list_positions(SESSION)[0].status is PaperPositionStatus.CLOSED
    store.close()


def test_missing_tick_quantity_never_fabricates_a_fill(tmp_path) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())

    assert _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=1), quantity=None, sequence=2),
    ) == ()
    assert store.list_fills(SESSION) == ()
    assert store.list_orders(SESSION)[0].status is PaperOrderStatus.PENDING
    store.close()


def test_second_entry_for_same_instrument_is_rejected(tmp_path) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())

    duplicate = engine.submit_entry(_intent(client_tag="signal-2"))

    assert not duplicate.accepted
    assert duplicate.order.rejection_reason is not None
    assert "duplicate_instrument_exposure" in duplicate.order.rejection_reason
    store.close()


def test_protective_stop_can_only_tighten(tmp_path) -> None:
    store, engine = _engine(tmp_path)
    engine.submit_entry(_intent())
    _ready_tick(engine, _tick(ENTRY_AT + timedelta(seconds=1), sequence=2))

    updated = engine.update_protective_stop(
        SESSION,
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        new_stop=Decimal("97"),
        now=ENTRY_AT + timedelta(seconds=2),
    )
    assert updated.protective_stop == Decimal("97")
    with pytest.raises(ValueError, match="only tighten"):
        engine.update_protective_stop(
            SESSION,
            exchange=Exchange.NSE_CM,
            symbol_token="3045",
            new_stop=Decimal("96"),
            now=ENTRY_AT + timedelta(seconds=3),
        )
    store.close()


def test_adverse_slippage_never_improves_simulated_fill(tmp_path) -> None:
    store, engine = _engine(
        tmp_path,
        config=_config(slippage_bps=Decimal("5")),
    )
    result = engine.submit_entry(_intent())
    assert result.order.reference_price == Decimal("100.05")

    fills = _ready_tick(
        engine,
        _tick(ENTRY_AT + timedelta(seconds=1), sequence=2),
    )
    assert fills[0].price == Decimal("100.05")
    store.close()
