"""Deterministic position sizing and portfolio/day entry limits."""

from __future__ import annotations

from datetime import time
from decimal import ROUND_FLOOR, Decimal

from .models import (
    PositionSizeRequest,
    RiskContext,
    RiskDecision,
    RiskLimits,
    RiskRejection,
    TradeSide,
)


class RiskEngine:
    def __init__(
        self,
        limits: RiskLimits,
        *,
        entry_start: time = time(9, 30),
        last_entry: time = time(14, 45),
    ) -> None:
        if entry_start >= last_entry:
            raise ValueError("entry_start must be before last_entry")
        self._limits = limits
        self._entry_start = entry_start
        self._last_entry = last_entry

    def evaluate(
        self, request: PositionSizeRequest, context: RiskContext
    ) -> RiskDecision:
        reasons = self._entry_rejections(request, context)
        entry = request.entry_price
        stop = request.stop_price
        risk_per_share = (
            abs(entry - stop)
            if entry.is_finite() and stop.is_finite()
            else Decimal(0)
        )
        conservative_equity = max(
            Decimal(0),
            min(context.starting_equity, context.current_equity),
        )
        risk_budget = conservative_equity * self._limits.risk_per_trade_pct
        quantity_by_risk = _floor_quantity(risk_budget, risk_per_share)

        capital_limit = (
            conservative_equity
            * self._limits.max_capital_utilization_pct
        )
        remaining_capital = max(Decimal(0), capital_limit - context.capital_reserved)
        if context.available_funds is not None:
            remaining_capital = min(remaining_capital, context.available_funds)
        quantity_by_capital = _floor_quantity(remaining_capital, entry)
        quantity_by_liquidity = request.max_liquidity_quantity or 0

        if not reasons:
            if quantity_by_risk < request.lot_size:
                reasons.append(RiskRejection.ZERO_RISK_QUANTITY)
            if quantity_by_capital < request.lot_size:
                reasons.append(RiskRejection.ZERO_CAPITAL_QUANTITY)
            if quantity_by_liquidity < request.lot_size:
                reasons.append(RiskRejection.ZERO_LIQUIDITY_QUANTITY)

        quantity = 0
        if not reasons:
            raw_quantity = min(
                quantity_by_risk,
                quantity_by_capital,
                quantity_by_liquidity,
            )
            quantity = raw_quantity - (raw_quantity % request.lot_size)
            if quantity < request.lot_size:
                reasons.append(RiskRejection.ZERO_RISK_QUANTITY)
                quantity = 0

        capital_required = entry * quantity if quantity else Decimal(0)
        planned_max_loss = risk_per_share * quantity if quantity else Decimal(0)
        return RiskDecision(
            approved=bool(quantity and not reasons),
            quantity=quantity,
            risk_budget=risk_budget,
            risk_per_share=risk_per_share,
            quantity_by_risk=quantity_by_risk,
            quantity_by_capital=quantity_by_capital,
            quantity_by_liquidity=quantity_by_liquidity,
            capital_required=capital_required,
            planned_max_loss=planned_max_loss,
            rejection_reasons=tuple(reasons),
        )

    def _entry_rejections(
        self, request: PositionSizeRequest, context: RiskContext
    ) -> list[RiskRejection]:
        reasons: list[RiskRejection] = []
        if not request.signal_qualified:
            reasons.append(RiskRejection.SIGNAL_NOT_QUALIFIED)
        if not request.market_data_trusted:
            reasons.append(RiskRejection.MARKET_DATA_UNTRUSTED)
        local_time = request.evaluated_at.timetz().replace(tzinfo=None)
        if not self._entry_start <= local_time <= self._last_entry:
            reasons.append(RiskRejection.OUTSIDE_ENTRY_WINDOW)
        if context.kill_switch_active:
            reasons.append(RiskRejection.KILL_SWITCH_ACTIVE)
        if context.state_uncertain:
            reasons.append(RiskRejection.STATE_UNCERTAIN)
        daily_loss = context.starting_equity * self._limits.max_daily_loss_pct
        if context.day_pnl <= -daily_loss:
            reasons.append(RiskRejection.DAILY_LOSS_LIMIT)
        if context.open_positions >= self._limits.max_open_positions:
            reasons.append(RiskRejection.MAX_OPEN_POSITIONS)
        if context.trades_today >= self._limits.max_trades_per_day:
            reasons.append(RiskRejection.MAX_TRADES_PER_DAY)
        if context.consecutive_losses >= self._limits.max_consecutive_losses:
            reasons.append(RiskRejection.MAX_CONSECUTIVE_LOSSES)
        if context.available_funds is None:
            reasons.append(RiskRejection.AVAILABLE_FUNDS_UNVERIFIED)
        if context.current_equity <= 0:
            reasons.append(RiskRejection.INVALID_EQUITY)
        if not request.entry_price.is_finite() or request.entry_price <= 0:
            reasons.append(RiskRejection.INVALID_ENTRY_PRICE)
        if not request.stop_price.is_finite() or request.stop_price <= 0:
            reasons.append(RiskRejection.INVALID_STOP_PRICE)
        if (
            request.entry_price.is_finite()
            and request.stop_price.is_finite()
            and (
                request.entry_price == request.stop_price
                or (
                    request.side is TradeSide.LONG
                    and request.stop_price >= request.entry_price
                )
                or (
                    request.side is TradeSide.SHORT
                    and request.stop_price <= request.entry_price
                )
            )
        ):
            reasons.append(RiskRejection.INVALID_STOP_DIRECTION)
        if request.lot_size < 1:
            reasons.append(RiskRejection.INVALID_LOT_SIZE)
        if request.max_liquidity_quantity is None:
            reasons.append(RiskRejection.LIQUIDITY_UNVERIFIED)
        return reasons


def _floor_quantity(numerator: Decimal, denominator: Decimal) -> int:
    if (
        not numerator.is_finite()
        or not denominator.is_finite()
        or numerator <= 0
        or denominator <= 0
    ):
        return 0
    return int((numerator / denominator).to_integral_value(rounding=ROUND_FLOOR))
