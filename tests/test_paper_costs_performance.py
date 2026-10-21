from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from trading_bot.domain import IST, Exchange
from trading_bot.paper import (
    IndianEquityIntradayCostModel,
    PaperFill,
    PaperOrder,
    PaperOrderPurpose,
    PaperOrderStatus,
    PaperPerformanceAnalyzer,
    PaperPosition,
    PaperPositionStatus,
    PaperReportStatus,
    TransactionAction,
)
from trading_bot.risk import TradeSide
from trading_bot.storage import SQLitePaperStore

SESSION = date(2026, 10, 21)
ENTRY_AT = datetime(2026, 10, 21, 10, 0, tzinfo=IST)
EXIT_AT = datetime(2026, 10, 21, 10, 5, tzinfo=IST)


def _order(
    order_id: str,
    *,
    purpose: PaperOrderPurpose,
    quantity: int = 100,
    side: TradeSide = TradeSide.LONG,
) -> PaperOrder:
    created = ENTRY_AT if purpose is PaperOrderPurpose.ENTRY else EXIT_AT
    return PaperOrder(
        order_id=order_id,
        client_tag=f"tag-{order_id}",
        session_date=SESSION,
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        side=side,
        purpose=purpose,
        requested_quantity=quantity,
        filled_quantity=quantity,
        reference_price=Decimal("100"),
        protective_stop=(
            Decimal("95") if purpose is PaperOrderPurpose.ENTRY else None
        ),
        planned_risk_per_share=(
            Decimal("5") if purpose is PaperOrderPurpose.ENTRY else None
        ),
        tick_size=Decimal("0.05"),
        lot_size=1,
        status=PaperOrderStatus.FILLED,
        created_at=created,
        updated_at=created,
    )


def _fill(order: PaperOrder, price: str, at: datetime) -> PaperFill:
    return PaperFill(
        fill_id=f"fill-{order.order_id}",
        order_id=order.order_id,
        session_date=SESSION,
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        side=order.side,
        quantity=order.requested_quantity,
        price=Decimal(price),
        filled_at=at,
    )


def test_cost_model_applies_buy_and_sell_levies_per_executed_order() -> None:
    model = IndianEquityIntradayCostModel()
    entry = _order("entry", purpose=PaperOrderPurpose.ENTRY)
    exit_order = _order("exit", purpose=PaperOrderPurpose.TIME_EXIT)

    buy = model.estimate_order(entry, (_fill(entry, "100", ENTRY_AT),))
    sell = model.estimate_order(
        exit_order, (_fill(exit_order, "110", EXIT_AT),)
    )

    assert buy.action is TransactionAction.BUY
    assert buy.turnover == Decimal("10000.00")
    assert buy.brokerage == Decimal("10.00")
    assert buy.stt == 0
    assert buy.stamp_duty == Decimal("0.30")
    assert buy.total == Decimal("12.48")
    assert sell.action is TransactionAction.SELL
    assert sell.brokerage == Decimal("11.00")
    assert sell.stt == Decimal("2.75")
    assert sell.stamp_duty == 0
    assert sell.total == Decimal("16.14")


def test_partial_fills_are_aggregated_before_brokerage_minimum_is_applied() -> None:
    model = IndianEquityIntradayCostModel()
    entry = _order("entry", purpose=PaperOrderPurpose.ENTRY, quantity=10)
    first = replace(_fill(entry, "100", ENTRY_AT), quantity=5, fill_id="first")
    second = replace(_fill(entry, "100", ENTRY_AT), quantity=5, fill_id="second")

    cost = model.estimate_order(entry, (first, second))

    assert cost.turnover == Decimal("1000.00")
    assert cost.brokerage == Decimal("5.00")


def test_short_entry_is_sell_and_short_exit_is_buy() -> None:
    model = IndianEquityIntradayCostModel()
    entry = _order(
        "short-entry",
        purpose=PaperOrderPurpose.ENTRY,
        side=TradeSide.SHORT,
    )
    exit_order = _order(
        "short-exit",
        purpose=PaperOrderPurpose.HARD_STOP,
        side=TradeSide.SHORT,
    )

    entry_cost = model.estimate_order(
        entry, (_fill(entry, "100", ENTRY_AT),)
    )
    exit_cost = model.estimate_order(
        exit_order, (_fill(exit_order, "101", EXIT_AT),)
    )

    assert entry_cost.action is TransactionAction.SELL
    assert entry_cost.stt > 0
    assert entry_cost.stamp_duty == 0
    assert exit_cost.action is TransactionAction.BUY
    assert exit_cost.stt == 0
    assert exit_cost.stamp_duty > 0


def test_performance_report_is_multi_session_ready_and_net_of_estimated_costs(
    tmp_path,
) -> None:
    store = SQLitePaperStore(f"sqlite:///{(tmp_path / 'report.db').as_posix()}")
    day = store.initialize_day(
        SESSION,
        starting_equity=Decimal("100000"),
        now=ENTRY_AT,
    )
    entry = _order("entry", purpose=PaperOrderPurpose.ENTRY, quantity=10)
    exit_order = _order("exit", purpose=PaperOrderPurpose.TIME_EXIT, quantity=10)
    entry_fill = _fill(entry, "100", ENTRY_AT)
    exit_fill = _fill(exit_order, "110", EXIT_AT)
    position = PaperPosition(
        session_date=SESSION,
        exchange=Exchange.NSE_CM,
        symbol_token="3045",
        trading_symbol="SBIN-EQ",
        side=TradeSide.LONG,
        entry_order_id=entry.order_id,
        initial_quantity=10,
        remaining_quantity=0,
        lot_size=1,
        average_entry_price=Decimal("100"),
        last_mark_price=Decimal("110"),
        last_mark_at=EXIT_AT,
        protective_stop=Decimal("95"),
        planned_risk_per_share=Decimal("5"),
        tick_size=Decimal("0.05"),
        realized_pnl=Decimal("100"),
        first_partial_started=False,
        second_partial_started=False,
        status=PaperPositionStatus.CLOSED,
        opened_at=ENTRY_AT,
        updated_at=EXIT_AT,
    )
    store.save_transition(
        day=replace(
            day,
            realized_pnl=Decimal("100"),
            trade_count=1,
            updated_at=EXIT_AT,
        ),
        orders=(entry, exit_order),
        positions=(position,),
        fills=(entry_fill, exit_fill),
        event_type="TEST_CLOSED_TRADE",
        occurred_at=EXIT_AT,
        entity_id=entry.order_id,
        details={},
    )

    report = PaperPerformanceAnalyzer(store).report(
        from_date=SESSION,
        to_date=SESSION,
    )

    assert report.status is PaperReportStatus.COMPLETE
    assert report.closed_trades == 1
    assert report.winning_trades == 1
    assert report.gross_realized_pnl == Decimal("100")
    assert report.estimated_costs_paid > 0
    assert report.estimated_net_realized_pnl < report.gross_realized_pnl
    assert report.win_rate == 1
    assert report.trades[0].planned_risk == Decimal("50")
    assert report.to_dict()["broker_orders_available"] is False
    store.close()
