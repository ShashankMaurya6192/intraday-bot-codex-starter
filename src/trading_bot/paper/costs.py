"""Configurable estimated Indian NSE cash-intraday transaction costs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from trading_bot.risk import TradeSide

from .models import PaperFill, PaperOrder, PaperOrderPurpose

_PAISE = Decimal("0.01")


class TransactionAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class IndianEquityIntradayCostSchedule:
    """Rate snapshot used for estimates; values are decimal fractions."""

    as_of: date = date(2026, 10, 21)
    brokerage_rate: Decimal = Decimal("0.001")
    brokerage_minimum_per_order: Decimal = Decimal("5")
    brokerage_maximum_per_order: Decimal = Decimal("20")
    exchange_transaction_rate: Decimal = Decimal("0.000030699")
    stt_sell_rate: Decimal = Decimal("0.00025")
    ipft_rate: Decimal = Decimal("0.000000001")
    sebi_turnover_rate: Decimal = Decimal("0.000001")
    stamp_duty_buy_rate: Decimal = Decimal("0.00003")
    gst_rate: Decimal = Decimal("0.18")
    schedule_name: str = "ANGEL_ONE_NSE_CASH_INTRADAY_ESTIMATE_V1"
    broker_source_url: str = "https://www.angelone.in/exchange-transaction-charges"
    statutory_source_url: str = (
        "https://www.nseindia.com/static/invest/"
        "first-time-investor-sebi-turnover-fees-stt-other-levies"
    )

    def __post_init__(self) -> None:
        for name in (
            "brokerage_rate",
            "brokerage_minimum_per_order",
            "brokerage_maximum_per_order",
            "exchange_transaction_rate",
            "stt_sell_rate",
            "ipft_rate",
            "sebi_turnover_rate",
            "stamp_duty_buy_rate",
            "gst_rate",
        ):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        if self.brokerage_minimum_per_order > self.brokerage_maximum_per_order:
            raise ValueError("brokerage minimum cannot exceed maximum")
        if any(
            getattr(self, name) > 1
            for name in (
                "brokerage_rate",
                "exchange_transaction_rate",
                "stt_sell_rate",
                "ipft_rate",
                "sebi_turnover_rate",
                "stamp_duty_buy_rate",
                "gst_rate",
            )
        ):
            raise ValueError("cost rates must be decimal fractions in [0, 1]")
        if not self.schedule_name.strip():
            raise ValueError("schedule_name must not be blank")

    @classmethod
    def zero(cls) -> IndianEquityIntradayCostSchedule:
        """Return a zero-cost schedule for focused execution tests only."""
        return cls(
            brokerage_rate=Decimal(0),
            brokerage_minimum_per_order=Decimal(0),
            brokerage_maximum_per_order=Decimal(0),
            exchange_transaction_rate=Decimal(0),
            stt_sell_rate=Decimal(0),
            ipft_rate=Decimal(0),
            sebi_turnover_rate=Decimal(0),
            stamp_duty_buy_rate=Decimal(0),
            gst_rate=Decimal(0),
            schedule_name="ZERO_COST_TEST_ONLY",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schedule_name": self.schedule_name,
            "as_of": self.as_of.isoformat(),
            "brokerage_rate": str(self.brokerage_rate),
            "brokerage_minimum_per_order": str(
                self.brokerage_minimum_per_order
            ),
            "brokerage_maximum_per_order": str(
                self.brokerage_maximum_per_order
            ),
            "exchange_transaction_rate": str(self.exchange_transaction_rate),
            "stt_sell_rate": str(self.stt_sell_rate),
            "ipft_rate": str(self.ipft_rate),
            "sebi_turnover_rate": str(self.sebi_turnover_rate),
            "stamp_duty_buy_rate": str(self.stamp_duty_buy_rate),
            "gst_rate": str(self.gst_rate),
            "broker_source_url": self.broker_source_url,
            "statutory_source_url": self.statutory_source_url,
            "estimate_only": True,
        }


@dataclass(frozen=True, slots=True)
class PaperOrderCost:
    order_id: str
    action: TransactionAction
    turnover: Decimal
    brokerage: Decimal
    exchange_transaction_charge: Decimal
    stt: Decimal
    ipft: Decimal
    sebi_turnover_charge: Decimal
    stamp_duty: Decimal
    gst: Decimal
    total: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "action": self.action.value,
            "turnover": str(self.turnover),
            "brokerage": str(self.brokerage),
            "exchange_transaction_charge": str(
                self.exchange_transaction_charge
            ),
            "stt": str(self.stt),
            "ipft": str(self.ipft),
            "sebi_turnover_charge": str(self.sebi_turnover_charge),
            "stamp_duty": str(self.stamp_duty),
            "gst": str(self.gst),
            "total": str(self.total),
        }


class IndianEquityIntradayCostModel:
    """Estimate charges per simulated executed order, including partial fills."""

    def __init__(
        self, schedule: IndianEquityIntradayCostSchedule | None = None
    ) -> None:
        self.schedule = schedule or IndianEquityIntradayCostSchedule()

    def estimate_order(
        self, order: PaperOrder, fills: tuple[PaperFill, ...]
    ) -> PaperOrderCost:
        if len({fill.fill_id for fill in fills}) != len(fills):
            raise ValueError("cost estimate received duplicate fill identities")
        if any(
            fill.order_id != order.order_id
            or fill.session_date != order.session_date
            or fill.exchange is not order.exchange
            or fill.symbol_token != order.symbol_token
            or fill.side is not order.side
            for fill in fills
        ):
            raise ValueError("cost estimate received a fill for another order")
        if sum((fill.quantity for fill in fills), 0) > order.filled_quantity:
            raise ValueError("cost estimate fill quantity exceeds the order state")
        turnover = sum(
            (fill.price * fill.quantity for fill in fills), Decimal(0)
        )
        action = _transaction_action(order)
        if turnover == 0:
            return PaperOrderCost(
                order_id=order.order_id,
                action=action,
                turnover=Decimal(0),
                brokerage=Decimal(0),
                exchange_transaction_charge=Decimal(0),
                stt=Decimal(0),
                ipft=Decimal(0),
                sebi_turnover_charge=Decimal(0),
                stamp_duty=Decimal(0),
                gst=Decimal(0),
                total=Decimal(0),
            )

        schedule = self.schedule
        brokerage = min(
            max(
                turnover * schedule.brokerage_rate,
                schedule.brokerage_minimum_per_order,
            ),
            schedule.brokerage_maximum_per_order,
        )
        exchange = turnover * schedule.exchange_transaction_rate
        stt = (
            turnover * schedule.stt_sell_rate
            if action is TransactionAction.SELL
            else Decimal(0)
        )
        ipft = turnover * schedule.ipft_rate
        sebi = turnover * schedule.sebi_turnover_rate
        stamp = (
            turnover * schedule.stamp_duty_buy_rate
            if action is TransactionAction.BUY
            else Decimal(0)
        )
        # NSE/Angel published rates do not fully describe contract-note rounding.
        # GST is estimated on brokerage, exchange transaction and SEBI charges.
        gst = (brokerage + exchange + sebi) * schedule.gst_rate
        rounded = tuple(
            _money(value)
            for value in (brokerage, exchange, stt, ipft, sebi, stamp, gst)
        )
        return PaperOrderCost(
            order_id=order.order_id,
            action=action,
            turnover=turnover.quantize(_PAISE, rounding=ROUND_HALF_UP),
            brokerage=rounded[0],
            exchange_transaction_charge=rounded[1],
            stt=rounded[2],
            ipft=rounded[3],
            sebi_turnover_charge=rounded[4],
            stamp_duty=rounded[5],
            gst=rounded[6],
            total=sum(rounded, Decimal(0)),
        )


def _transaction_action(order: PaperOrder) -> TransactionAction:
    is_entry = order.purpose is PaperOrderPurpose.ENTRY
    is_buy = (
        order.side is TradeSide.LONG
        if is_entry
        else order.side is TradeSide.SHORT
    )
    return TransactionAction.BUY if is_buy else TransactionAction.SELL


def _money(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)
