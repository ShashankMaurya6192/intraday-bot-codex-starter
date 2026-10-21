"""Deterministic multi-session performance reports for persisted PAPER state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from .costs import IndianEquityIntradayCostModel, PaperOrderCost
from .models import PaperOrderPurpose, PaperPositionStatus

if TYPE_CHECKING:
    from trading_bot.storage.paper import SQLitePaperStore


class PaperReportStatus(StrEnum):
    NO_DATA = "NO_DATA"
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class PaperTradePerformance:
    session_date: date
    trading_symbol: str
    side: str
    opened_at: datetime
    closed_at: datetime
    initial_quantity: int
    planned_risk: Decimal
    gross_pnl: Decimal
    estimated_costs: Decimal
    estimated_net_pnl: Decimal
    net_r_multiple: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "session_date": self.session_date.isoformat(),
            "trading_symbol": self.trading_symbol,
            "side": self.side,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "initial_quantity": self.initial_quantity,
            "planned_risk": str(self.planned_risk),
            "gross_pnl": str(self.gross_pnl),
            "estimated_costs": str(self.estimated_costs),
            "estimated_net_pnl": str(self.estimated_net_pnl),
            "net_r_multiple": str(self.net_r_multiple),
        }


@dataclass(frozen=True, slots=True)
class PaperPerformanceReport:
    status: PaperReportStatus
    from_date: date
    to_date: date
    session_count: int
    closed_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    open_positions: int
    active_orders: int
    rejected_entries: int
    uncertain_sessions: int
    uncertainty_events: int
    kill_switch_sessions: int
    gross_realized_pnl: Decimal
    estimated_costs_paid: Decimal
    estimated_net_realized_pnl: Decimal
    win_rate: Decimal | None
    expectancy_per_closed_trade: Decimal | None
    average_net_r: Decimal | None
    profit_factor: Decimal | None
    maximum_closed_trade_drawdown: Decimal
    order_costs: tuple[PaperOrderCost, ...]
    trades: tuple[PaperTradePerformance, ...]
    cost_schedule: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "mode": "PAPER",
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "session_count": self.session_count,
            "closed_trades": self.closed_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "breakeven_trades": self.breakeven_trades,
            "open_positions": self.open_positions,
            "active_orders": self.active_orders,
            "rejected_entries": self.rejected_entries,
            "uncertain_sessions": self.uncertain_sessions,
            "uncertainty_events": self.uncertainty_events,
            "kill_switch_sessions": self.kill_switch_sessions,
            "gross_realized_pnl": str(self.gross_realized_pnl),
            "estimated_costs_paid": str(self.estimated_costs_paid),
            "estimated_net_realized_pnl": str(
                self.estimated_net_realized_pnl
            ),
            "win_rate": _optional_decimal(self.win_rate),
            "expectancy_per_closed_trade": _optional_decimal(
                self.expectancy_per_closed_trade
            ),
            "average_net_r": _optional_decimal(self.average_net_r),
            "profit_factor": _optional_decimal(self.profit_factor),
            "maximum_closed_trade_drawdown": str(
                self.maximum_closed_trade_drawdown
            ),
            "cost_schedule": self.cost_schedule,
            "cost_basis": "ESTIMATE_ONLY_NOT_A_CONTRACT_NOTE",
            "trades": [item.to_dict() for item in self.trades],
            "order_costs": [item.to_dict() for item in self.order_costs],
            "broker_orders_available": False,
        }


class PaperPerformanceAnalyzer:
    def __init__(
        self,
        store: SQLitePaperStore,
        cost_model: IndianEquityIntradayCostModel | None = None,
    ) -> None:
        self._store = store
        self._cost_model = cost_model or IndianEquityIntradayCostModel()

    def report(self, *, from_date: date, to_date: date) -> PaperPerformanceReport:
        if to_date < from_date:
            raise ValueError("to_date must not precede from_date")
        days = self._store.list_days(from_date=from_date, to_date=to_date)
        all_costs: list[PaperOrderCost] = []
        trades: list[PaperTradePerformance] = []
        open_positions = 0
        active_orders = 0
        rejected_entries = 0
        uncertain_sessions = 0
        uncertainty_events = 0
        kill_switch_sessions = 0

        for day in days:
            orders = self._store.list_orders(day.session_date)
            fills = self._store.list_fills(day.session_date)
            positions = self._store.list_positions(day.session_date)
            audit_events = self._store.list_audit_events(day.session_date)
            fills_by_order = {
                order.order_id: tuple(
                    fill for fill in fills if fill.order_id == order.order_id
                )
                for order in orders
            }
            costs_by_order: dict[str, PaperOrderCost] = {}
            for order in orders:
                cost = self._cost_model.estimate_order(
                    order, fills_by_order[order.order_id]
                )
                if cost.turnover > 0:
                    all_costs.append(cost)
                    costs_by_order[order.order_id] = cost
            open_positions += sum(
                position.status is PaperPositionStatus.OPEN
                for position in positions
            )
            active_orders += sum(order.active for order in orders)
            rejected_entries += sum(
                order.purpose is PaperOrderPurpose.ENTRY
                and order.status.value == "REJECTED"
                for order in orders
            )
            uncertain_sessions += int(day.state_uncertain)
            uncertainty_events += sum(
                event.event_type == "PAPER_STATE_UNCERTAIN"
                for event in audit_events
            )
            kill_switch_sessions += int(day.kill_switch_active)

            for position in positions:
                if position.status is not PaperPositionStatus.CLOSED:
                    continue
                position_orders = tuple(
                    order
                    for order in orders
                    if order.exchange is position.exchange
                    and order.symbol_token == position.symbol_token
                )
                position_fills = tuple(
                    fill
                    for order in position_orders
                    for fill in fills_by_order[order.order_id]
                )
                exit_fills = tuple(
                    fill
                    for order in position_orders
                    if order.purpose is not PaperOrderPurpose.ENTRY
                    for fill in fills_by_order[order.order_id]
                )
                if not position_fills or not exit_fills:
                    continue
                costs = sum(
                    (
                        costs_by_order[order.order_id].total
                        for order in position_orders
                        if order.order_id in costs_by_order
                    ),
                    Decimal(0),
                )
                planned_risk = (
                    position.planned_risk_per_share * position.initial_quantity
                )
                net_pnl = position.realized_pnl - costs
                trades.append(
                    PaperTradePerformance(
                        session_date=day.session_date,
                        trading_symbol=position.trading_symbol,
                        side=position.side.value,
                        opened_at=position.opened_at,
                        closed_at=max(fill.filled_at for fill in exit_fills),
                        initial_quantity=position.initial_quantity,
                        planned_risk=planned_risk,
                        gross_pnl=position.realized_pnl,
                        estimated_costs=costs,
                        estimated_net_pnl=net_pnl,
                        net_r_multiple=net_pnl / planned_risk,
                    )
                )

        trades.sort(key=lambda item: (item.closed_at, item.trading_symbol))
        all_costs.sort(key=lambda item: item.order_id)
        gross = sum((day.realized_pnl for day in days), Decimal(0))
        total_costs = sum((item.total for item in all_costs), Decimal(0))
        net = gross - total_costs
        winners = tuple(item for item in trades if item.estimated_net_pnl > 0)
        losers = tuple(item for item in trades if item.estimated_net_pnl < 0)
        breakeven = len(trades) - len(winners) - len(losers)
        if not days:
            status = PaperReportStatus.NO_DATA
        elif open_positions or active_orders or uncertain_sessions:
            status = PaperReportStatus.INCOMPLETE
        else:
            status = PaperReportStatus.COMPLETE
        trade_count = len(trades)
        negative_total = abs(
            sum((item.estimated_net_pnl for item in losers), Decimal(0))
        )
        positive_total = sum(
            (item.estimated_net_pnl for item in winners), Decimal(0)
        )
        return PaperPerformanceReport(
            status=status,
            from_date=from_date,
            to_date=to_date,
            session_count=len(days),
            closed_trades=trade_count,
            winning_trades=len(winners),
            losing_trades=len(losers),
            breakeven_trades=breakeven,
            open_positions=open_positions,
            active_orders=active_orders,
            rejected_entries=rejected_entries,
            uncertain_sessions=uncertain_sessions,
            uncertainty_events=uncertainty_events,
            kill_switch_sessions=kill_switch_sessions,
            gross_realized_pnl=gross,
            estimated_costs_paid=total_costs,
            estimated_net_realized_pnl=net,
            win_rate=(
                None if trade_count == 0 else Decimal(len(winners)) / trade_count
            ),
            expectancy_per_closed_trade=(
                None
                if trade_count == 0
                else sum(
                    (item.estimated_net_pnl for item in trades), Decimal(0)
                )
                / trade_count
            ),
            average_net_r=(
                None
                if trade_count == 0
                else sum((item.net_r_multiple for item in trades), Decimal(0))
                / trade_count
            ),
            profit_factor=(
                None if negative_total == 0 else positive_total / negative_total
            ),
            maximum_closed_trade_drawdown=_maximum_drawdown(trades),
            order_costs=tuple(all_costs),
            trades=tuple(trades),
            cost_schedule=self._cost_model.schedule.to_dict(),
        )


def _maximum_drawdown(trades: list[PaperTradePerformance]) -> Decimal:
    running = Decimal(0)
    peak = Decimal(0)
    maximum = Decimal(0)
    for trade in trades:
        running += trade.estimated_net_pnl
        peak = max(peak, running)
        maximum = max(maximum, peak - running)
    return maximum


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
