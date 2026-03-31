"""
Order manager service.

Handles bracket order placement and tracks the full order lifecycle from
submission through entry fill to exit (SL or TP hit). Persists trade records
to the database.

Usage::

    om = OrderManager(broker, session_factory)
    broker.on_order_update(om.on_order_update)
    result = await om.place_bracket(signal, quantity=1)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.broker_client import BrokerClient
from backend.core.models import (
    BracketOrderRequest,
    BracketOrderResult,
    Direction,
    OrderStatus,
    OrderUpdate,
    Signal,
)
from backend.database.models import TradeRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tick conventions — tick size (points) and dollar value per tick per contract
# ---------------------------------------------------------------------------

_TICK_CONVENTIONS: dict[str, dict[str, float]] = {
    "ES":  {"tick_size": 0.25, "tick_value": 12.50},
    "MES": {"tick_size": 0.25, "tick_value": 1.25},
    "NQ":  {"tick_size": 0.25, "tick_value": 5.00},
    "MNQ": {"tick_size": 0.25, "tick_value": 0.50},
}

_DEFAULT_TICK_CONVENTION: dict[str, float] = {"tick_size": 0.25, "tick_value": 12.50}


def _uid() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------


@dataclass
class _ActiveBracket:
    """Tracks an open bracket order from placement through exit."""

    signal: Signal
    result: BracketOrderResult
    quantity: int
    fill_price: float | None = None
    fill_timestamp: datetime | None = None
    trade_record_id: str | None = None


# ---------------------------------------------------------------------------
# OrderManager
# ---------------------------------------------------------------------------


class OrderManager:
    """Bracket order placement and lifecycle management.

    Registers as an order-update listener on the broker so it can track each
    leg (entry, SL, TP) through to completion and persist trade records.
    """

    def __init__(
        self,
        broker: BrokerClient,
        session_factory: async_sessionmaker[AsyncSession],
        tick_conventions: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self._broker = broker
        self._session_factory = session_factory
        self._tick_conventions = tick_conventions or _TICK_CONVENTIONS

        # entry_order_id → _ActiveBracket
        self._orders: dict[str, _ActiveBracket] = {}
        # sl_order_id or tp_order_id → entry_order_id (reverse lookup)
        self._exit_order_map: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def place_bracket(self, signal: Signal, quantity: int) -> BracketOrderResult:
        """Submit a bracket order for an approved signal.

        Builds a :class:`BracketOrderRequest` from the signal, submits it to
        the broker, and registers the returned IDs for lifecycle tracking.
        """
        request = BracketOrderRequest(
            instrument=signal.instrument,
            direction=signal.direction,
            quantity=quantity,
            entry_price=signal.entry_price,
            stop_loss_price=signal.stop_loss_price,
            take_profit_price=signal.take_profit_price,
        )

        result = await self._broker.place_bracket_order(request)

        bracket = _ActiveBracket(signal=signal, result=result, quantity=quantity)
        self._orders[result.entry_order_id] = bracket
        self._exit_order_map[result.stop_loss_order_id] = result.entry_order_id
        self._exit_order_map[result.take_profit_order_id] = result.entry_order_id

        logger.info(
            "Bracket placed: signal=%s %s %s x%d  entry=%s sl=%s tp=%s",
            signal.id,
            signal.direction.value,
            signal.instrument,
            quantity,
            result.entry_order_id,
            result.stop_loss_order_id,
            result.take_profit_order_id,
        )
        return result

    async def on_order_update(self, update: OrderUpdate) -> None:
        """Receive an order status event from the broker.

        Routes the update to the correct handler depending on whether the
        order ID belongs to an entry leg or an exit (SL/TP) leg.
        """
        order_id = update.order_id

        if order_id in self._orders:
            await self._handle_entry_update(update, self._orders[order_id])
        elif order_id in self._exit_order_map:
            entry_id = self._exit_order_map[order_id]
            bracket = self._orders.get(entry_id)
            if bracket is not None:
                await self._handle_exit_update(update, bracket)
        else:
            logger.debug(
                "on_order_update: unknown order_id=%s status=%s",
                order_id,
                update.status.value,
            )

    def active_orders(self) -> dict[str, tuple[Signal, BracketOrderResult]]:
        """Snapshot of all live brackets, keyed by entry_order_id."""
        return {
            entry_id: (b.signal, b.result)
            for entry_id, b in self._orders.items()
        }

    def is_flat(self) -> bool:
        """True when no bracket orders are being tracked."""
        return len(self._orders) == 0

    # ------------------------------------------------------------------
    # Entry / exit handlers
    # ------------------------------------------------------------------

    async def _handle_entry_update(
        self, update: OrderUpdate, bracket: _ActiveBracket
    ) -> None:
        if update.status == OrderStatus.FILLED:
            bracket.fill_price = update.fill_price
            bracket.fill_timestamp = update.timestamp
            logger.info(
                "Entry filled: order=%s %s %s x%d @ %.4f",
                update.order_id,
                update.direction.value,
                update.instrument,
                update.filled_quantity,
                update.fill_price,
            )
            await self._persist_entry_fill(update, bracket)

        elif update.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            logger.warning(
                "Entry %s: order=%s reason=%s",
                update.status.value.lower(),
                update.order_id,
                update.message or update.status.value,
            )
            self._remove_bracket(bracket.result)

    async def _handle_exit_update(
        self, update: OrderUpdate, bracket: _ActiveBracket
    ) -> None:
        if update.status == OrderStatus.FILLED:
            is_sl = update.order_id == bracket.result.stop_loss_order_id
            leg_name = "SL" if is_sl else "TP"

            fill_price = bracket.fill_price if bracket.fill_price is not None else bracket.signal.entry_price
            actual_pnl = self._compute_pnl(
                direction=bracket.signal.direction,
                fill_price=fill_price,
                exit_price=update.fill_price,
                quantity=bracket.quantity,
                instrument=bracket.signal.instrument,
            )

            logger.info(
                "%s hit: order=%s %s @ %.4f  pnl=%.2f",
                leg_name,
                update.order_id,
                update.instrument,
                update.fill_price,
                actual_pnl,
            )

            await self._persist_exit_fill(update, bracket, actual_pnl)
            self._remove_bracket(bracket.result)

        elif update.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            logger.warning(
                "Exit order %s: order=%s reason=%s",
                update.status.value.lower(),
                update.order_id,
                update.message or update.status.value,
            )
            self._remove_bracket(bracket.result)

    # ------------------------------------------------------------------
    # Database persistence
    # ------------------------------------------------------------------

    async def _persist_entry_fill(
        self, update: OrderUpdate, bracket: _ActiveBracket
    ) -> None:
        """Insert a TradeRecord when the entry leg fills."""
        signal = bracket.signal
        trade_id = _uid()
        bracket.trade_record_id = trade_id
        slippage = abs(update.fill_price - signal.entry_price)

        async with self._session_factory() as session:
            trade = TradeRecord(
                id=trade_id,
                signal_id=signal.id,
                order_id=update.order_id,
                fill_price=update.fill_price,
                fill_timestamp=update.timestamp,
                slippage=slippage,
            )
            session.add(trade)
            await session.commit()

        logger.debug(
            "TradeRecord created: id=%s signal=%s fill=%.4f",
            trade_id,
            signal.id,
            update.fill_price,
        )

    async def _persist_exit_fill(
        self,
        update: OrderUpdate,
        bracket: _ActiveBracket,
        actual_pnl: float,
    ) -> None:
        """Update the TradeRecord with exit price, P&L, and duration."""
        if bracket.trade_record_id is None:
            logger.warning(
                "No TradeRecord for entry order %s; cannot persist exit.",
                bracket.result.entry_order_id,
            )
            return

        ref_timestamp = bracket.fill_timestamp or bracket.signal.timestamp
        duration = (update.timestamp - ref_timestamp).total_seconds()

        async with self._session_factory() as session:
            trade = await session.get(TradeRecord, bracket.trade_record_id)
            if trade is not None:
                trade.exit_price = update.fill_price
                trade.exit_timestamp = update.timestamp
                trade.actual_pnl = actual_pnl
                trade.duration_seconds = duration
                await session.commit()
                logger.debug(
                    "TradeRecord updated: id=%s pnl=%.2f duration=%.1fs",
                    bracket.trade_record_id,
                    actual_pnl,
                    duration,
                )
            else:
                logger.error(
                    "TradeRecord not found: id=%s", bracket.trade_record_id
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_pnl(
        self,
        direction: Direction,
        fill_price: float,
        exit_price: float,
        quantity: int,
        instrument: str,
    ) -> float:
        """Realized P&L in dollars using instrument tick conventions."""
        conv = self._tick_conventions.get(instrument, _DEFAULT_TICK_CONVENTION)
        tick_size = conv["tick_size"]
        tick_value = conv["tick_value"]

        price_diff = exit_price - fill_price
        if direction == Direction.SHORT:
            price_diff = -price_diff
        ticks = price_diff / tick_size
        return ticks * tick_value * quantity

    def _remove_bracket(self, result: BracketOrderResult) -> None:
        """Remove a bracket from the registry and exit-order map."""
        self._orders.pop(result.entry_order_id, None)
        self._exit_order_map.pop(result.stop_loss_order_id, None)
        self._exit_order_map.pop(result.take_profit_order_id, None)
