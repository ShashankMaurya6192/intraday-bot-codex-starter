"""Fail-closed, broker-free paper execution driven by trusted market ticks."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from trading_bot.domain import IST, Exchange, MarketTick
from trading_bot.marketdata import MarketDataHealthStatus
from trading_bot.risk import (
    PositionSizeRequest,
    RiskContext,
    RiskEngine,
    RiskLimits,
    TradeSide,
)

from .models import (
    PaperFill,
    PaperOrder,
    PaperOrderPurpose,
    PaperOrderStatus,
    PaperPosition,
    PaperPositionStatus,
    PaperSubmissionResult,
    PaperTradeIntent,
    PaperTradingDay,
)

if TYPE_CHECKING:
    from trading_bot.storage.paper import SQLitePaperStore


@dataclass(frozen=True, slots=True)
class PaperExecutionConfig:
    slippage_bps: Decimal = Decimal("5")
    tick_participation_pct: Decimal = Decimal("0.10")
    first_partial_pct: Decimal = Decimal("0.50")
    second_partial_pct: Decimal = Decimal("0.25")
    square_off_time: time = time(15, 15)
    stale_after_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in (
            "slippage_bps",
            "tick_participation_pct",
            "first_partial_pct",
            "second_partial_pct",
        ):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, value)
        if not Decimal(0) <= self.slippage_bps <= Decimal(100):
            raise ValueError("slippage_bps must be in [0, 100]")
        if not Decimal(0) < self.tick_participation_pct <= Decimal(1):
            raise ValueError("tick_participation_pct must be in (0, 1]")
        if self.first_partial_pct < 0 or self.second_partial_pct < 0:
            raise ValueError("partial percentages must be non-negative")
        if self.first_partial_pct + self.second_partial_pct > 1:
            raise ValueError("partial percentages cannot exceed one")
        if self.first_partial_pct == 0 and self.second_partial_pct > 0:
            raise ValueError("second partial requires a non-zero first partial")
        if self.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")


class PaperExecutionEngine:
    """Simulate fills without exposing any live broker order capability."""

    def __init__(
        self,
        store: SQLitePaperStore,
        limits: RiskLimits,
        *,
        config: PaperExecutionConfig | None = None,
        entry_start: time = time(9, 30),
        last_entry: time = time(14, 45),
    ) -> None:
        self._store = store
        self._limits = limits
        self._config = config or PaperExecutionConfig()
        if entry_start < time(9, 30) or last_entry > time(14, 45):
            raise ValueError("paper entry window must stay within 09:30-14:45 IST")
        if not last_entry < self._config.square_off_time < time(15, 30):
            raise ValueError("paper square-off must be after entries and before 15:30")
        self._risk = RiskEngine(
            limits,
            entry_start=entry_start,
            last_entry=last_entry,
        )
        self._lock = threading.RLock()
        self._marks: dict[
            tuple[date, Exchange, str], tuple[Decimal, datetime]
        ] = {}

    def initialize_day(
        self,
        session_date: date,
        *,
        starting_equity: Decimal,
        now: datetime,
    ) -> PaperTradingDay:
        return self._store.initialize_day(
            session_date,
            starting_equity=starting_equity,
            now=_ist(now),
        )

    def submit_entry(self, intent: PaperTradeIntent) -> PaperSubmissionResult:
        """Persist an idempotent entry request after all risk gates pass."""
        with self._lock:
            existing = self._store.load_order_by_client_tag(intent.client_tag)
            if existing is not None:
                return PaperSubmissionResult(
                    accepted=existing.status
                    not in {PaperOrderStatus.REJECTED, PaperOrderStatus.CANCELLED},
                    order=existing,
                    risk_decision=None,
                    idempotent_replay=True,
                )
            day = self._require_day(intent.session_date)
            orders = list(self._store.list_orders(intent.session_date))
            positions = list(self._store.list_positions(intent.session_date))
            duplicate = any(
                item.exchange is intent.exchange
                and item.symbol_token == intent.symbol_token
                for item in positions
            ) or any(
                item.exchange is intent.exchange
                and item.symbol_token == intent.symbol_token
                and item.purpose is PaperOrderPurpose.ENTRY
                and item.active
                for item in orders
            )
            context = self._risk_context(day, positions, intent.submitted_at)
            entry_price = _slipped_price(
                intent.reference_entry_price,
                is_buy=intent.side is TradeSide.LONG,
                slippage_bps=self._config.slippage_bps,
                tick_size=intent.tick_size,
            )
            decision = self._risk.evaluate(
                PositionSizeRequest(
                    side=intent.side,
                    entry_price=entry_price,
                    stop_price=intent.protective_stop,
                    lot_size=intent.lot_size,
                    max_liquidity_quantity=intent.max_liquidity_quantity,
                    evaluated_at=intent.submitted_at,
                    signal_qualified=intent.signal_qualified and not duplicate,
                    market_data_trusted=intent.market_data_trusted,
                ),
                context,
            )
            rejection_reasons = [item.value for item in decision.rejection_reasons]
            if duplicate:
                rejection_reasons.insert(0, "duplicate_instrument_exposure")
            accepted = decision.approved and not duplicate
            order = PaperOrder(
                order_id=_stable_id("entry", intent.session_date, intent.client_tag),
                client_tag=intent.client_tag,
                session_date=intent.session_date,
                exchange=intent.exchange,
                symbol_token=intent.symbol_token,
                trading_symbol=intent.trading_symbol,
                side=intent.side,
                purpose=PaperOrderPurpose.ENTRY,
                requested_quantity=decision.quantity if accepted else 0,
                filled_quantity=0,
                reference_price=entry_price,
                protective_stop=intent.protective_stop,
                planned_risk_per_share=(
                    decision.risk_per_share if decision.risk_per_share > 0 else None
                ),
                tick_size=intent.tick_size,
                lot_size=intent.lot_size,
                status=(
                    PaperOrderStatus.PENDING
                    if accepted
                    else PaperOrderStatus.REJECTED
                ),
                created_at=intent.submitted_at,
                updated_at=intent.submitted_at,
                rejection_reason=(
                    None if accepted else ",".join(dict.fromkeys(rejection_reasons))
                ),
            )
            next_orders = [*orders, order]
            next_day = replace(
                day,
                capital_reserved=_capital_reserved(next_orders, positions),
                trade_count=day.trade_count + int(accepted),
                updated_at=intent.submitted_at,
            )
            self._store.save_transition(
                day=next_day,
                orders=(order,),
                event_type="ENTRY_ACCEPTED" if accepted else "ENTRY_REJECTED",
                occurred_at=intent.submitted_at,
                entity_id=order.order_id,
                details={
                    "client_tag": intent.client_tag,
                    "risk": decision.to_dict(),
                    "rejection_reasons": list(dict.fromkeys(rejection_reasons)),
                },
            )
            return PaperSubmissionResult(
                accepted=accepted,
                order=order,
                risk_decision=decision,
            )

    def on_tick(
        self,
        tick: MarketTick,
        *,
        health_status: MarketDataHealthStatus,
        observed_at: datetime | None = None,
    ) -> tuple[PaperFill, ...]:
        """Advance at most one simulated order from a trusted tick."""
        now = _ist(observed_at or datetime.now(IST))
        session_date = tick.received_at.date()
        with self._lock:
            day = self._store.load_day(session_date)
            if day is None:
                return ()
            orders = list(self._store.list_orders(session_date))
            positions = list(self._store.list_positions(session_date))
            if not _trusted_tick(
                tick,
                health_status=health_status,
                observed_at=now,
                stale_after_seconds=self._config.stale_after_seconds,
            ):
                relevant_exposure = any(
                    item.exchange is tick.exchange
                    and item.symbol_token == tick.symbol_token
                    and item.active
                    for item in orders
                ) or any(
                    item.exchange is tick.exchange
                    and item.symbol_token == tick.symbol_token
                    and item.status is PaperPositionStatus.OPEN
                    for item in positions
                )
                if relevant_exposure:
                    self._fail_closed(
                        day,
                        now=now,
                        reason="untrusted_or_stale_tick",
                    )
                return ()
            self._marks[(session_date, tick.exchange, tick.symbol_token)] = (
                tick.last_traded_price,
                tick.received_at,
            )
            if day.state_uncertain:
                return ()

            position = _open_position(positions, tick.exchange, tick.symbol_token)
            active_entry = _active_order(
                orders,
                tick.exchange,
                tick.symbol_token,
                purposes={PaperOrderPurpose.ENTRY},
            )
            active_exit = _active_order(
                orders,
                tick.exchange,
                tick.symbol_token,
                purposes=set(PaperOrderPurpose) - {PaperOrderPurpose.ENTRY},
            )

            if position is not None:
                marked_position = replace(
                    position,
                    last_mark_price=tick.last_traded_price,
                    last_mark_at=tick.received_at,
                    updated_at=tick.received_at,
                )
                force_purpose = self._forced_exit_purpose(day, marked_position, tick)
                if force_purpose is not None:
                    if active_exit is not None and active_exit.purpose in {
                        PaperOrderPurpose.HARD_STOP,
                        PaperOrderPurpose.TIME_EXIT,
                        PaperOrderPurpose.KILL_SWITCH_EXIT,
                    }:
                        return self._fill_order(
                            day,
                            active_exit,
                            marked_position,
                            tick,
                            orders,
                            positions,
                        )
                    changed_orders: list[PaperOrder] = []
                    if active_entry is not None:
                        cancelled = replace(
                            active_entry,
                            status=PaperOrderStatus.CANCELLED,
                            updated_at=tick.received_at,
                            rejection_reason="protective_exit_triggered",
                        )
                        _replace_order(orders, cancelled)
                        changed_orders.append(cancelled)
                    if active_exit is not None:
                        cancelled = replace(
                            active_exit,
                            status=PaperOrderStatus.CANCELLED,
                            updated_at=tick.received_at,
                            rejection_reason="superseded_by_protective_exit",
                        )
                        _replace_order(orders, cancelled)
                        changed_orders.append(cancelled)
                    exit_order = self._new_exit_order(
                        marked_position,
                        purpose=force_purpose,
                        quantity=marked_position.remaining_quantity,
                        reference_price=tick.last_traded_price,
                        now=tick.received_at,
                    )
                    orders.append(exit_order)
                    changed_orders.append(exit_order)
                    _replace_position(positions, marked_position)
                    next_day = replace(
                        day,
                        capital_reserved=_capital_reserved(orders, positions),
                        updated_at=tick.received_at,
                    )
                    self._store.save_transition(
                        day=next_day,
                        orders=changed_orders,
                        positions=(marked_position,),
                        event_type="PROTECTIVE_EXIT_TRIGGERED",
                        occurred_at=tick.received_at,
                        entity_id=exit_order.order_id,
                        details={"purpose": force_purpose.value},
                    )
                    return ()

                if active_exit is not None:
                    return self._fill_order(
                        day,
                        active_exit,
                        marked_position,
                        tick,
                        orders,
                        positions,
                    )
                if active_entry is not None:
                    return self._fill_order(
                        day,
                        active_entry,
                        marked_position,
                        tick,
                        orders,
                        positions,
                    )
                target = self._target_order(marked_position, tick)
                if target is not None:
                    _replace_position(positions, target[1])
                    orders.append(target[0])
                    next_day = replace(
                        day,
                        capital_reserved=_capital_reserved(orders, positions),
                        updated_at=tick.received_at,
                    )
                    self._store.save_transition(
                        day=next_day,
                        orders=(target[0],),
                        positions=(target[1],),
                        event_type="PARTIAL_EXIT_TRIGGERED",
                        occurred_at=tick.received_at,
                        entity_id=target[0].order_id,
                        details={"purpose": target[0].purpose.value},
                    )
                return ()

            if active_entry is not None:
                return self._fill_order(
                    day,
                    active_entry,
                    None,
                    tick,
                    orders,
                    positions,
                )
            return ()

    def activate_kill_switch(
        self,
        session_date: date,
        *,
        now: datetime,
        reason: str,
    ) -> PaperTradingDay:
        """Block entries and queue exits for every open paper position."""
        now = _ist(now)
        if not reason.strip():
            raise ValueError("kill-switch reason must not be blank")
        with self._lock:
            day = self._require_day(session_date)
            orders = list(self._store.list_orders(session_date))
            positions = list(self._store.list_positions(session_date))
            changed: list[PaperOrder] = []
            for order in tuple(orders):
                if (
                    order.active
                    and order.purpose is not PaperOrderPurpose.KILL_SWITCH_EXIT
                ):
                    cancelled = replace(
                        order,
                        status=PaperOrderStatus.CANCELLED,
                        updated_at=now,
                        rejection_reason="kill_switch_activated",
                    )
                    _replace_order(orders, cancelled)
                    changed.append(cancelled)
            for position in positions:
                if position.status is not PaperPositionStatus.OPEN:
                    continue
                existing_exit = _active_order(
                    orders,
                    position.exchange,
                    position.symbol_token,
                    purposes={PaperOrderPurpose.KILL_SWITCH_EXIT},
                )
                if existing_exit is not None:
                    continue
                exit_order = self._new_exit_order(
                    position,
                    purpose=PaperOrderPurpose.KILL_SWITCH_EXIT,
                    quantity=position.remaining_quantity,
                    reference_price=position.last_mark_price,
                    now=now,
                )
                orders.append(exit_order)
                changed.append(exit_order)
            next_day = replace(
                day,
                kill_switch_active=True,
                capital_reserved=_capital_reserved(orders, positions),
                updated_at=now,
            )
            self._store.save_transition(
                day=next_day,
                orders=changed,
                event_type="KILL_SWITCH_ACTIVATED",
                occurred_at=now,
                entity_id=None,
                details={"reason": reason},
            )
            return next_day

    def mark_state_uncertain(
        self,
        session_date: date,
        *,
        now: datetime,
        reason: str,
    ) -> None:
        """Fail closed after a feed, queue, writer, or reconciliation failure."""
        if not reason.strip():
            raise ValueError("uncertainty reason must not be blank")
        with self._lock:
            self._fail_closed(
                self._require_day(session_date),
                now=_ist(now),
                reason=reason,
            )

    def update_protective_stop(
        self,
        session_date: date,
        *,
        exchange: Exchange,
        symbol_token: str,
        new_stop: Decimal,
        now: datetime,
    ) -> PaperPosition:
        """Tighten a stop; loosening risk is deliberately unsupported."""
        now = _ist(now)
        new_stop = Decimal(str(new_stop))
        if not new_stop.is_finite() or new_stop <= 0:
            raise ValueError("new stop must be finite and positive")
        with self._lock:
            day = self._require_day(session_date)
            positions = list(self._store.list_positions(session_date))
            position = _open_position(positions, Exchange(exchange), symbol_token)
            if position is None:
                raise ValueError("open paper position not found")
            if (
                position.side is TradeSide.LONG
                and new_stop < position.protective_stop
            ) or (
                position.side is TradeSide.SHORT
                and new_stop > position.protective_stop
            ):
                raise ValueError("protective stops may only tighten")
            updated = replace(position, protective_stop=new_stop, updated_at=now)
            self._store.save_transition(
                day=replace(day, updated_at=now),
                positions=(updated,),
                event_type="PROTECTIVE_STOP_UPDATED",
                occurred_at=now,
                entity_id=position.entry_order_id,
                details={
                    "previous_stop": str(position.protective_stop),
                    "new_stop": str(new_stop),
                },
            )
            return updated

    def reconcile_flat_day(
        self, session_date: date, *, now: datetime
    ) -> PaperTradingDay:
        """Clear uncertainty only when no simulated exposure can remain."""
        now = _ist(now)
        with self._lock:
            day = self._require_day(session_date)
            orders = self._store.list_orders(session_date)
            positions = self._store.list_positions(session_date)
            if any(item.active for item in orders) or any(
                item.status is PaperPositionStatus.OPEN for item in positions
            ):
                raise RuntimeError("cannot reconcile an uncertain day with exposure")
            reconciled = replace(day, state_uncertain=False, updated_at=now)
            self._store.save_transition(
                day=reconciled,
                event_type="FLAT_STATE_RECONCILED",
                occurred_at=now,
                entity_id=None,
                details={},
            )
            return reconciled

    def _fill_order(
        self,
        day: PaperTradingDay,
        order: PaperOrder,
        position: PaperPosition | None,
        tick: MarketTick,
        orders: list[PaperOrder],
        positions: list[PaperPosition],
    ) -> tuple[PaperFill, ...]:
        if tick.received_at <= order.created_at:
            return ()
        fill_id = _tick_fill_id(order, tick)
        if self._store.has_fill(fill_id):
            return ()
        quantity = _fillable_quantity(
            order,
            last_traded_quantity=tick.last_traded_quantity,
            participation=self._config.tick_participation_pct,
        )
        if quantity == 0:
            return ()
        is_entry = order.purpose is PaperOrderPurpose.ENTRY
        is_buy = (
            order.side is TradeSide.LONG
            if is_entry
            else order.side is TradeSide.SHORT
        )
        price = _slipped_price(
            tick.last_traded_price,
            is_buy=is_buy,
            slippage_bps=self._config.slippage_bps,
            tick_size=order.tick_size,
        )
        if is_entry and not self._entry_fill_is_within_plan(day, order, price):
            cancelled = replace(
                order,
                status=PaperOrderStatus.CANCELLED,
                updated_at=tick.received_at,
                rejection_reason="entry_price_exceeds_risk_or_capital_plan",
            )
            _replace_order(orders, cancelled)
            changed_positions: tuple[PaperPosition, ...] = ()
            if position is not None:
                _replace_position(positions, position)
                changed_positions = (position,)
            next_day = replace(
                day,
                capital_reserved=_capital_reserved(orders, positions),
                updated_at=tick.received_at,
            )
            self._store.save_transition(
                day=next_day,
                orders=(cancelled,),
                positions=changed_positions,
                event_type="ENTRY_CANCELLED_PRICE_DRIFT",
                occurred_at=tick.received_at,
                entity_id=order.order_id,
                details={
                    "planned_entry": str(order.reference_price),
                    "observed_simulated_fill": str(price),
                },
            )
            return ()
        new_filled = order.filled_quantity + quantity
        filled_order = replace(
            order,
            filled_quantity=new_filled,
            status=(
                PaperOrderStatus.FILLED
                if new_filled == order.requested_quantity
                else PaperOrderStatus.PARTIALLY_FILLED
            ),
            updated_at=tick.received_at,
        )
        _replace_order(orders, filled_order)
        fill = PaperFill(
            fill_id=fill_id,
            order_id=order.order_id,
            session_date=order.session_date,
            exchange=order.exchange,
            symbol_token=order.symbol_token,
            side=order.side,
            quantity=quantity,
            price=price,
            filled_at=tick.received_at,
        )
        if is_entry:
            changed_position = self._apply_entry_fill(position, order, fill, tick)
            event_type = "ENTRY_FILL"
        else:
            if position is None:
                self._fail_closed(
                    day,
                    now=tick.received_at,
                    reason="exit_order_without_open_position",
                )
                return ()
            changed_position = self._apply_exit_fill(position, fill, tick)
            event_type = "EXIT_FILL"
        _replace_position(positions, changed_position)
        realized_delta = (
            Decimal(0)
            if is_entry
            else changed_position.realized_pnl - position.realized_pnl
        )
        consecutive_losses = day.consecutive_losses
        if (
            not is_entry
            and changed_position.status is PaperPositionStatus.CLOSED
        ):
            consecutive_losses = (
                day.consecutive_losses + 1
                if changed_position.realized_pnl < 0
                else 0
            )
        next_day = replace(
            day,
            realized_pnl=day.realized_pnl + realized_delta,
            consecutive_losses=consecutive_losses,
            capital_reserved=_capital_reserved(orders, positions),
            updated_at=tick.received_at,
        )
        if (
            next_day.realized_pnl
            <= -(next_day.starting_equity * self._limits.max_daily_loss_pct)
            or next_day.consecutive_losses >= self._limits.max_consecutive_losses
        ):
            next_day = replace(next_day, kill_switch_active=True)
        self._store.save_transition(
            day=next_day,
            orders=(filled_order,),
            positions=(changed_position,),
            fills=(fill,),
            event_type=event_type,
            occurred_at=tick.received_at,
            entity_id=order.order_id,
            details={
                "fill_id": fill.fill_id,
                "quantity": fill.quantity,
                "price": str(fill.price),
                "simulation_model": fill.simulation_model,
            },
        )
        return (fill,)

    def _entry_fill_is_within_plan(
        self,
        day: PaperTradingDay,
        order: PaperOrder,
        fill_price: Decimal,
    ) -> bool:
        if order.protective_stop is None or order.planned_risk_per_share is None:
            return False
        direction_valid = (
            fill_price > order.protective_stop
            if order.side is TradeSide.LONG
            else fill_price < order.protective_stop
        )
        if not direction_valid:
            return False
        actual_risk_per_share = abs(fill_price - order.protective_stop)
        if actual_risk_per_share > order.planned_risk_per_share:
            return False
        current_order_reserve = order.reference_price * order.remaining_quantity
        other_reserved = max(
            Decimal(0),
            day.capital_reserved - current_order_reserve,
        )
        prospective_capital = (
            other_reserved + fill_price * order.remaining_quantity
        )
        conservative_equity = max(
            Decimal(0),
            min(day.starting_equity, day.starting_equity + day.realized_pnl),
        )
        capital_limit = (
            conservative_equity * self._limits.max_capital_utilization_pct
        )
        return prospective_capital <= capital_limit

    def _apply_entry_fill(
        self,
        position: PaperPosition | None,
        order: PaperOrder,
        fill: PaperFill,
        tick: MarketTick,
    ) -> PaperPosition:
        if order.protective_stop is None or order.planned_risk_per_share is None:
            raise RuntimeError("accepted entry lacks protective risk state")
        if position is None:
            return PaperPosition(
                session_date=order.session_date,
                exchange=order.exchange,
                symbol_token=order.symbol_token,
                trading_symbol=order.trading_symbol,
                side=order.side,
                entry_order_id=order.order_id,
                initial_quantity=fill.quantity,
                remaining_quantity=fill.quantity,
                lot_size=order.lot_size,
                average_entry_price=fill.price,
                last_mark_price=tick.last_traded_price,
                last_mark_at=tick.received_at,
                protective_stop=order.protective_stop,
                planned_risk_per_share=order.planned_risk_per_share,
                tick_size=order.tick_size,
                realized_pnl=Decimal(0),
                first_partial_started=False,
                second_partial_started=False,
                status=PaperPositionStatus.OPEN,
                opened_at=fill.filled_at,
                updated_at=fill.filled_at,
            )
        total_quantity = position.initial_quantity + fill.quantity
        average = (
            position.average_entry_price * position.initial_quantity
            + fill.price * fill.quantity
        ) / total_quantity
        return replace(
            position,
            initial_quantity=total_quantity,
            remaining_quantity=position.remaining_quantity + fill.quantity,
            average_entry_price=average,
            last_mark_price=tick.last_traded_price,
            last_mark_at=tick.received_at,
            updated_at=tick.received_at,
        )

    def _apply_exit_fill(
        self,
        position: PaperPosition,
        fill: PaperFill,
        tick: MarketTick,
    ) -> PaperPosition:
        if fill.quantity > position.remaining_quantity:
            raise RuntimeError("paper exit fill exceeds open quantity")
        pnl_per_share = (
            fill.price - position.average_entry_price
            if position.side is TradeSide.LONG
            else position.average_entry_price - fill.price
        )
        remaining = position.remaining_quantity - fill.quantity
        return replace(
            position,
            remaining_quantity=remaining,
            last_mark_price=tick.last_traded_price,
            last_mark_at=tick.received_at,
            realized_pnl=position.realized_pnl + pnl_per_share * fill.quantity,
            status=(
                PaperPositionStatus.CLOSED
                if remaining == 0
                else PaperPositionStatus.OPEN
            ),
            updated_at=tick.received_at,
        )

    def _forced_exit_purpose(
        self,
        day: PaperTradingDay,
        position: PaperPosition,
        tick: MarketTick,
    ) -> PaperOrderPurpose | None:
        if day.kill_switch_active:
            return PaperOrderPurpose.KILL_SWITCH_EXIT
        local_time = tick.exchange_timestamp.timetz().replace(tzinfo=None)
        if local_time >= self._config.square_off_time:
            return PaperOrderPurpose.TIME_EXIT
        stop_hit = (
            tick.last_traded_price <= position.protective_stop
            if position.side is TradeSide.LONG
            else tick.last_traded_price >= position.protective_stop
        )
        return PaperOrderPurpose.HARD_STOP if stop_hit else None

    def _target_order(
        self,
        position: PaperPosition,
        tick: MarketTick,
    ) -> tuple[PaperOrder, PaperPosition] | None:
        one_r = (
            position.average_entry_price + position.planned_risk_per_share
            if position.side is TradeSide.LONG
            else position.average_entry_price - position.planned_risk_per_share
        )
        one_point_five_r = (
            position.average_entry_price
            + Decimal("1.5") * position.planned_risk_per_share
            if position.side is TradeSide.LONG
            else position.average_entry_price
            - Decimal("1.5") * position.planned_risk_per_share
        )
        if not position.first_partial_started and _target_hit(
            position.side, tick.last_traded_price, one_r
        ):
            quantity = _partial_quantity(
                position.initial_quantity,
                position.remaining_quantity,
                self._config.first_partial_pct,
                lot_size=position.lot_size,
            )
            if quantity:
                updated = replace(
                    position,
                    first_partial_started=True,
                    last_mark_price=tick.last_traded_price,
                    last_mark_at=tick.received_at,
                    updated_at=tick.received_at,
                )
                return (
                    self._new_exit_order(
                        updated,
                        purpose=PaperOrderPurpose.PARTIAL_ONE_R,
                        quantity=quantity,
                        reference_price=tick.last_traded_price,
                        now=tick.received_at,
                    ),
                    updated,
                )
        if (
            position.first_partial_started
            and not position.second_partial_started
            and _target_hit(
                position.side,
                tick.last_traded_price,
                one_point_five_r,
            )
        ):
            quantity = _partial_quantity(
                position.initial_quantity,
                position.remaining_quantity,
                self._config.second_partial_pct,
                lot_size=position.lot_size,
            )
            if quantity:
                updated = replace(
                    position,
                    second_partial_started=True,
                    last_mark_price=tick.last_traded_price,
                    last_mark_at=tick.received_at,
                    updated_at=tick.received_at,
                )
                return (
                    self._new_exit_order(
                        updated,
                        purpose=PaperOrderPurpose.PARTIAL_ONE_POINT_FIVE_R,
                        quantity=quantity,
                        reference_price=tick.last_traded_price,
                        now=tick.received_at,
                    ),
                    updated,
                )
        return None

    def _new_exit_order(
        self,
        position: PaperPosition,
        *,
        purpose: PaperOrderPurpose,
        quantity: int,
        reference_price: Decimal,
        now: datetime,
    ) -> PaperOrder:
        client_tag = f"{position.entry_order_id[:32]}:{purpose.value}"
        return PaperOrder(
            order_id=_stable_id(
                "exit",
                position.session_date,
                client_tag,
            ),
            client_tag=client_tag,
            session_date=position.session_date,
            exchange=position.exchange,
            symbol_token=position.symbol_token,
            trading_symbol=position.trading_symbol,
            side=position.side,
            purpose=purpose,
            requested_quantity=quantity,
            filled_quantity=0,
            reference_price=reference_price,
            protective_stop=position.protective_stop,
            planned_risk_per_share=position.planned_risk_per_share,
            tick_size=position.tick_size,
            lot_size=position.lot_size,
            status=PaperOrderStatus.PENDING,
            created_at=now,
            updated_at=now,
        )

    def _risk_context(
        self,
        day: PaperTradingDay,
        positions: list[PaperPosition],
        now: datetime,
    ) -> RiskContext:
        unrealized = Decimal(0)
        marks_trusted = True
        open_count = 0
        for position in positions:
            if position.status is not PaperPositionStatus.OPEN:
                continue
            open_count += 1
            mark, marked_at = self._marks.get(
                (position.session_date, position.exchange, position.symbol_token),
                (position.last_mark_price, position.last_mark_at),
            )
            age = (now - marked_at).total_seconds()
            if age < 0 or age > self._config.stale_after_seconds:
                marks_trusted = False
            unrealized += (
                (mark - position.average_entry_price) * position.remaining_quantity
                if position.side is TradeSide.LONG
                else (position.average_entry_price - mark)
                * position.remaining_quantity
            )
        funds = max(
            Decimal(0),
            day.starting_equity
            + day.realized_pnl
            + min(Decimal(0), unrealized)
            - day.capital_reserved,
        )
        return RiskContext(
            starting_equity=day.starting_equity,
            realized_pnl=day.realized_pnl,
            unrealized_pnl=unrealized,
            capital_reserved=day.capital_reserved,
            available_funds=funds,
            open_positions=open_count,
            trades_today=day.trade_count,
            consecutive_losses=day.consecutive_losses,
            kill_switch_active=day.kill_switch_active,
            state_uncertain=day.state_uncertain or not marks_trusted,
        )

    def _fail_closed(
        self,
        day: PaperTradingDay,
        *,
        now: datetime,
        reason: str,
    ) -> None:
        orders = list(self._store.list_orders(day.session_date))
        positions = list(self._store.list_positions(day.session_date))
        changed: list[PaperOrder] = []
        for order in tuple(orders):
            if order.purpose is PaperOrderPurpose.ENTRY and order.active:
                cancelled = replace(
                    order,
                    status=PaperOrderStatus.CANCELLED,
                    updated_at=now,
                    rejection_reason=reason,
                )
                _replace_order(orders, cancelled)
                changed.append(cancelled)
        if day.state_uncertain and not changed:
            return
        unsafe = replace(
            day,
            state_uncertain=True,
            capital_reserved=_capital_reserved(orders, positions),
            updated_at=now,
        )
        self._store.save_transition(
            day=unsafe,
            orders=changed,
            event_type="PAPER_STATE_UNCERTAIN",
            occurred_at=now,
            entity_id=None,
            details={"reason": reason},
        )

    def _require_day(self, session_date: date) -> PaperTradingDay:
        day = self._store.load_day(session_date)
        if day is None:
            raise RuntimeError("paper trading day is not initialized")
        return day


def _trusted_tick(
    tick: MarketTick,
    *,
    health_status: MarketDataHealthStatus,
    observed_at: datetime,
    stale_after_seconds: float,
) -> bool:
    age = (observed_at - tick.received_at).total_seconds()
    exchange_age = (observed_at - tick.exchange_timestamp).total_seconds()
    return (
        health_status is MarketDataHealthStatus.READY
        and 0 <= age <= stale_after_seconds
        and 0 <= exchange_age <= stale_after_seconds
    )


def _slipped_price(
    price: Decimal,
    *,
    is_buy: bool,
    slippage_bps: Decimal,
    tick_size: Decimal,
) -> Decimal:
    factor = Decimal(1) + (
        slippage_bps / Decimal(10_000)
        if is_buy
        else -(slippage_bps / Decimal(10_000))
    )
    raw = price * factor
    rounding = ROUND_CEILING if is_buy else ROUND_FLOOR
    ticks = (raw / tick_size).to_integral_value(rounding=rounding)
    return max(tick_size, ticks * tick_size)


def _fillable_quantity(
    order: PaperOrder,
    *,
    last_traded_quantity: int | None,
    participation: Decimal,
) -> int:
    if last_traded_quantity is None or last_traded_quantity <= 0:
        return 0
    capacity = int(
        (Decimal(last_traded_quantity) * participation).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    capacity -= capacity % order.lot_size
    quantity = min(order.remaining_quantity, capacity)
    return quantity - (quantity % order.lot_size)


def _capital_reserved(
    orders: list[PaperOrder], positions: list[PaperPosition]
) -> Decimal:
    position_capital = sum(
        (
            item.average_entry_price * item.remaining_quantity
            for item in positions
            if item.status is PaperPositionStatus.OPEN
        ),
        start=Decimal(0),
    )
    pending_entry_capital = sum(
        (
            item.reference_price * item.remaining_quantity
            for item in orders
            if item.purpose is PaperOrderPurpose.ENTRY and item.active
        ),
        start=Decimal(0),
    )
    return position_capital + pending_entry_capital


def _open_position(
    positions: list[PaperPosition], exchange: Exchange, symbol_token: str
) -> PaperPosition | None:
    return next(
        (
            item
            for item in positions
            if item.exchange is exchange
            and item.symbol_token == symbol_token
            and item.status is PaperPositionStatus.OPEN
        ),
        None,
    )


def _active_order(
    orders: list[PaperOrder],
    exchange: Exchange,
    symbol_token: str,
    *,
    purposes: set[PaperOrderPurpose],
) -> PaperOrder | None:
    matches = [
        item
        for item in orders
        if item.exchange is exchange
        and item.symbol_token == symbol_token
        and item.purpose in purposes
        and item.active
    ]
    if len(matches) > 1:
        raise RuntimeError("multiple active paper orders for one instrument")
    return matches[0] if matches else None


def _replace_order(orders: list[PaperOrder], changed: PaperOrder) -> None:
    for index, order in enumerate(orders):
        if order.order_id == changed.order_id:
            orders[index] = changed
            return
    orders.append(changed)


def _replace_position(
    positions: list[PaperPosition], changed: PaperPosition
) -> None:
    for index, position in enumerate(positions):
        if (
            position.session_date == changed.session_date
            and position.exchange is changed.exchange
            and position.symbol_token == changed.symbol_token
        ):
            positions[index] = changed
            return
    positions.append(changed)


def _partial_quantity(
    initial_quantity: int,
    remaining_quantity: int,
    percentage: Decimal,
    *,
    lot_size: int,
) -> int:
    raw = int(
        (Decimal(initial_quantity) * percentage).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    quantity = min(raw - (raw % lot_size), remaining_quantity)
    return quantity - (quantity % lot_size)


def _target_hit(side: TradeSide, ltp: Decimal, target: Decimal) -> bool:
    return ltp >= target if side is TradeSide.LONG else ltp <= target


def _stable_id(kind: str, session_date: date, identity: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"paper:{kind}:{session_date}:{identity}"))


def _tick_fill_id(order: PaperOrder, tick: MarketTick) -> str:
    identity = ":".join(
        (
            order.order_id,
            str(tick.sequence_number),
            tick.exchange_timestamp.isoformat(),
            tick.received_at.isoformat(),
        )
    )
    return str(uuid5(NAMESPACE_URL, f"paper:fill:{identity}"))


def _ist(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("paper timestamp must be timezone-aware")
    return value.astimezone(IST)
