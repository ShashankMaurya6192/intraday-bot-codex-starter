from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from trading_bot.domain import IST
from trading_bot.risk import (
    PositionSizeRequest,
    RiskContext,
    RiskEngine,
    RiskLimits,
    RiskRejection,
    TradeSide,
)


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


def _request(**changes: object) -> PositionSizeRequest:
    values: dict[str, object] = {
        "side": TradeSide.LONG,
        "entry_price": Decimal("100"),
        "stop_price": Decimal("95"),
        "lot_size": 1,
        "max_liquidity_quantity": 1_000,
        "evaluated_at": datetime(2026, 10, 21, 10, 0, tzinfo=IST),
        "signal_qualified": True,
        "market_data_trusted": True,
    }
    values.update(changes)
    return PositionSizeRequest(**values)  # type: ignore[arg-type]


def _context(**changes: object) -> RiskContext:
    values: dict[str, object] = {
        "starting_equity": Decimal("100000"),
        "realized_pnl": Decimal(0),
        "unrealized_pnl": Decimal(0),
        "capital_reserved": Decimal(0),
        "available_funds": Decimal("100000"),
        "open_positions": 0,
        "trades_today": 0,
        "consecutive_losses": 0,
        "kill_switch_active": False,
        "state_uncertain": False,
    }
    values.update(changes)
    return RiskContext(**values)  # type: ignore[arg-type]


def test_position_size_is_minimum_of_risk_capital_and_liquidity() -> None:
    engine = RiskEngine(
        _limits(
            risk_per_trade_pct=Decimal("0.01"),
            max_daily_loss_pct=Decimal("0.02"),
            max_capital_utilization_pct=Decimal("0.50"),
        )
    )

    decision = engine.evaluate(
        _request(lot_size=5, max_liquidity_quantity=83),
        _context(),
    )

    assert decision.approved
    assert decision.quantity_by_risk == 200
    assert decision.quantity_by_capital == 500
    assert decision.quantity_by_liquidity == 83
    assert decision.quantity == 80
    assert decision.planned_max_loss == Decimal("400")
    assert decision.capital_required == Decimal("8000")


@pytest.mark.parametrize(
    ("size_request", "context", "reason"),
    [
        (
            _request(signal_qualified=False),
            _context(),
            RiskRejection.SIGNAL_NOT_QUALIFIED,
        ),
        (
            _request(market_data_trusted=False),
            _context(),
            RiskRejection.MARKET_DATA_UNTRUSTED,
        ),
        (
            _request(evaluated_at=datetime(2026, 10, 21, 9, 29, tzinfo=IST)),
            _context(),
            RiskRejection.OUTSIDE_ENTRY_WINDOW,
        ),
        (
            _request(),
            _context(kill_switch_active=True),
            RiskRejection.KILL_SWITCH_ACTIVE,
        ),
        (_request(), _context(state_uncertain=True), RiskRejection.STATE_UNCERTAIN),
        (
            _request(),
            _context(realized_pnl=Decimal("-900")),
            RiskRejection.DAILY_LOSS_LIMIT,
        ),
        (_request(), _context(open_positions=2), RiskRejection.MAX_OPEN_POSITIONS),
        (_request(), _context(trades_today=4), RiskRejection.MAX_TRADES_PER_DAY),
        (
            _request(),
            _context(consecutive_losses=3),
            RiskRejection.MAX_CONSECUTIVE_LOSSES,
        ),
        (
            _request(),
            _context(available_funds=None),
            RiskRejection.AVAILABLE_FUNDS_UNVERIFIED,
        ),
        (
            _request(stop_price=Decimal("100")),
            _context(),
            RiskRejection.INVALID_STOP_DIRECTION,
        ),
        (
            _request(max_liquidity_quantity=None),
            _context(),
            RiskRejection.LIQUIDITY_UNVERIFIED,
        ),
    ],
)
def test_entry_safety_gates_fail_closed(
    size_request: PositionSizeRequest,
    context: RiskContext,
    reason: RiskRejection,
) -> None:
    decision = RiskEngine(_limits()).evaluate(size_request, context)

    assert not decision.approved
    assert decision.quantity == 0
    assert reason in decision.rejection_reasons


def test_short_stop_must_be_above_entry() -> None:
    decision = RiskEngine(_limits()).evaluate(
        replace(
            _request(),
            side=TradeSide.SHORT,
            stop_price=Decimal("99"),
        ),
        _context(),
    )

    assert RiskRejection.INVALID_STOP_DIRECTION in decision.rejection_reasons


def test_lot_rounding_cannot_exceed_any_limit() -> None:
    decision = RiskEngine(_limits()).evaluate(
        _request(lot_size=25, max_liquidity_quantity=59),
        _context(),
    )

    assert decision.approved
    assert decision.quantity == 50


def test_losses_reduce_sizing_equity_but_profits_do_not_increase_it() -> None:
    engine = RiskEngine(_limits())

    after_loss = engine.evaluate(
        _request(),
        _context(realized_pnl=Decimal("-100")),
    )
    after_profit = engine.evaluate(
        _request(),
        _context(realized_pnl=Decimal("100")),
    )

    assert after_loss.risk_budget == Decimal("299.700")
    assert after_loss.quantity == 59
    assert after_profit.risk_budget == Decimal("300")
    assert after_profit.quantity == 60


def test_invalid_risk_limit_relationship_is_rejected() -> None:
    with pytest.raises(ValueError, match="risk per trade"):
        _limits(
            risk_per_trade_pct=Decimal("0.02"),
            max_daily_loss_pct=Decimal("0.01"),
        )
