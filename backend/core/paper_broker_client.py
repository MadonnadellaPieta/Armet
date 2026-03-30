"""
Paper trading broker client.

Simulates order execution without connecting to any real broker.
Used for:
- Paper trading mode (spec requirement: default mode on first launch)
- Testing strategies and the signal pipeline
- Development without broker credentials

Simulates fills at the requested entry price with configurable slippage.
Tracks positions in-memory and calculates P&L against live tick data.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from backend.core.broker_client import (
    BrokerClient,
    ConnectionCallback,
    OrderBookCallback,
    OrderError,
    OrderUpdateCallback,
    TickCallback,
)
from backend.core.models import (
    AccountInfo,
    BracketOrderRequest,
    BracketOrderResult,
    ConnectionStatus,
    Direction,
    OrderBook,
    OrderBookLevel,
    OrderStatus,
    OrderUpdate,
    Position,
    Tick,
)

logger = logging.getLogger(__name__)


def _uid() -> str:
    return uuid.uuid4().hex


class PaperBrokerClient(BrokerClient):
    """In-memory paper trading broker.

    Simulates order fills, tracks positions, and monitors SL/TP levels
    against incoming tick data to resolve bracket orders.
    """

    def __init__(
        self,
        initial_balance: float = 100_000.0,
        slippage_ticks: float = 0.0,
        tick_size: float = 0.25,
        tick_value: float = 12.50,
    ) -> None:
        self._initial_balance = initial_balance
        self._balance = initial_balance
        self._daily_pnl = 0.0
        self._slippage_ticks = slippage_ticks
        self._tick_size = tick_size
        self._tick_value = tick_value

        self._connected = False
        self._connection_status = ConnectionStatus.DISCONNECTED

        # Callback registries
        self._tick_callbacks: dict[str, TickCallback] = {}
        self._order_book_callbacks: dict[str, OrderBookCallback] = {}
        self._order_update_callbacks: list[OrderUpdateCallback] = []
        self._connection_callbacks: list[ConnectionCallback] = []

        # Order tracking
        self._pending_orders: dict[str, _PaperOrder] = {}
        self._positions: dict[str, Position] = {}

        # Last prices per instrument (for P&L calculation)
        self._last_prices: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._connected = True
        await self._set_connection_status(ConnectionStatus.CONNECTED)
        logger.info("Paper broker connected (balance=%.2f)", self._balance)

    async def disconnect(self) -> None:
        self._connected = False
        await self._set_connection_status(ConnectionStatus.DISCONNECTED)

    async def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def subscribe_market_data(
        self, instrument: str, on_tick: TickCallback
    ) -> None:
        self._tick_callbacks[instrument] = on_tick
        logger.info("Paper broker subscribed to market data: %s", instrument)

    async def unsubscribe_market_data(self, instrument: str) -> None:
        self._tick_callbacks.pop(instrument, None)

    async def subscribe_order_book(
        self, instrument: str, on_update: OrderBookCallback
    ) -> None:
        self._order_book_callbacks[instrument] = on_update

    async def unsubscribe_order_book(self, instrument: str) -> None:
        self._order_book_callbacks.pop(instrument, None)

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def place_bracket_order(
        self, request: BracketOrderRequest
    ) -> BracketOrderResult:
        entry_id = f"paper_entry_{_uid()}"
        sl_id = f"paper_sl_{_uid()}"
        tp_id = f"paper_tp_{_uid()}"

        # Simulate immediate fill at entry price (market order)
        slippage = self._slippage_ticks * self._tick_size
        if request.direction == Direction.LONG:
            fill_price = request.entry_price + slippage
        else:
            fill_price = request.entry_price - slippage

        now = datetime.utcnow()

        # Create position
        position = Position(
            instrument=request.instrument,
            direction=request.direction,
            quantity=request.quantity,
            entry_price=fill_price,
            entry_timestamp=now,
            current_price=fill_price,
            unrealized_pnl=0.0,
            order_id=entry_id,
        )
        self._positions[entry_id] = position

        # Track SL/TP for this position
        self._pending_orders[entry_id] = _PaperOrder(
            entry_order_id=entry_id,
            sl_order_id=sl_id,
            tp_order_id=tp_id,
            instrument=request.instrument,
            direction=request.direction,
            quantity=request.quantity,
            fill_price=fill_price,
            sl_price=request.stop_loss_price,
            tp_price=request.take_profit_price,
        )

        # Emit entry fill notification
        entry_update = OrderUpdate(
            order_id=entry_id,
            status=OrderStatus.FILLED,
            instrument=request.instrument,
            direction=request.direction,
            quantity=request.quantity,
            filled_quantity=request.quantity,
            fill_price=fill_price,
            timestamp=now,
            message="Paper fill",
        )
        await self._emit_order_update(entry_update)

        logger.info(
            "Paper bracket order filled: %s %s %s x%d @ %.2f (SL=%.2f, TP=%.2f)",
            entry_id, request.direction.value, request.instrument,
            request.quantity, fill_price, request.stop_loss_price,
            request.take_profit_price,
        )

        return BracketOrderResult(
            entry_order_id=entry_id,
            stop_loss_order_id=sl_id,
            take_profit_order_id=tp_id,
            status=OrderStatus.FILLED,
        )

    async def cancel_order(self, order_id: str) -> bool:
        if order_id in self._pending_orders:
            del self._pending_orders[order_id]
            return True
        return False

    async def modify_order(
        self,
        order_id: str,
        new_price: float | None = None,
        new_quantity: int | None = None,
    ) -> bool:
        order = self._pending_orders.get(order_id)
        if order is None:
            return False
        if new_quantity is not None:
            order.quantity = new_quantity
        return True

    async def flatten_all(self) -> int:
        count = len(self._positions)
        for entry_id, pos in list(self._positions.items()):
            last_price = self._last_prices.get(pos.instrument, pos.entry_price)
            pnl = self._calculate_pnl(pos, last_price)
            self._balance += pnl
            self._daily_pnl += pnl

            # Emit close update
            await self._emit_order_update(OrderUpdate(
                order_id=entry_id,
                status=OrderStatus.FILLED,
                instrument=pos.instrument,
                direction=pos.direction,
                quantity=pos.quantity,
                filled_quantity=pos.quantity,
                fill_price=last_price,
                message="Flattened",
            ))

        self._positions.clear()
        self._pending_orders.clear()
        logger.info("Paper broker flattened %d positions", count)
        return count

    # ------------------------------------------------------------------
    # Position & account queries
    # ------------------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def get_account_info(self) -> AccountInfo:
        unrealized = sum(
            self._calculate_pnl(
                pos, self._last_prices.get(pos.instrument, pos.entry_price)
            )
            for pos in self._positions.values()
        )
        return AccountInfo(
            account_id="PAPER",
            balance=self._balance,
            equity=self._balance + unrealized,
            daily_pnl=self._daily_pnl + unrealized,
            eod_threshold=self._initial_balance - 3000,
            buying_power=self._balance + unrealized,
            open_positions_count=len(self._positions),
        )

    # ------------------------------------------------------------------
    # Event registration
    # ------------------------------------------------------------------

    def on_order_update(self, callback: OrderUpdateCallback) -> None:
        self._order_update_callbacks.append(callback)

    def on_connection_status_change(self, callback: ConnectionCallback) -> None:
        self._connection_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Tick injection (for feeding simulated/live data into paper mode)
    # ------------------------------------------------------------------

    async def inject_tick(self, tick: Tick) -> None:
        """Feed a tick into the paper broker.

        Updates position P&L and checks SL/TP levels.
        Also forwards to any registered tick callbacks.
        """
        self._last_prices[tick.instrument] = tick.price

        # Update position P&L
        for pos in self._positions.values():
            if pos.instrument == tick.instrument:
                pos.current_price = tick.price
                pos.unrealized_pnl = self._calculate_pnl(pos, tick.price)

        # Check SL/TP hits
        await self._check_bracket_exits(tick)

        # Forward to subscribed callbacks
        callback = self._tick_callbacks.get(tick.instrument)
        if callback is not None:
            await callback(tick)

    async def inject_order_book(self, order_book: OrderBook) -> None:
        """Feed an order book update into the paper broker."""
        callback = self._order_book_callbacks.get(order_book.instrument)
        if callback is not None:
            await callback(order_book)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _check_bracket_exits(self, tick: Tick) -> None:
        """Check if any pending SL/TP orders should be triggered."""
        to_close: list[str] = []

        for entry_id, order in self._pending_orders.items():
            if order.instrument != tick.instrument:
                continue

            hit_sl = False
            hit_tp = False

            if order.direction == Direction.LONG:
                hit_sl = tick.price <= order.sl_price
                hit_tp = tick.price >= order.tp_price
            else:
                hit_sl = tick.price >= order.sl_price
                hit_tp = tick.price <= order.tp_price

            if hit_sl or hit_tp:
                exit_price = order.sl_price if hit_sl else order.tp_price
                exit_type = "SL" if hit_sl else "TP"
                pos = self._positions.get(entry_id)
                if pos is not None:
                    pnl = self._calculate_pnl(pos, exit_price)
                    self._balance += pnl
                    self._daily_pnl += pnl

                    await self._emit_order_update(OrderUpdate(
                        order_id=order.sl_order_id if hit_sl else order.tp_order_id,
                        status=OrderStatus.FILLED,
                        instrument=order.instrument,
                        direction=order.direction,
                        quantity=order.quantity,
                        filled_quantity=order.quantity,
                        fill_price=exit_price,
                        message=f"Paper {exit_type} hit",
                        parent_order_id=entry_id,
                    ))

                    logger.info(
                        "Paper %s hit: %s %s @ %.2f (P&L: %.2f)",
                        exit_type, entry_id, order.instrument, exit_price, pnl,
                    )

                to_close.append(entry_id)

        for entry_id in to_close:
            self._positions.pop(entry_id, None)
            self._pending_orders.pop(entry_id, None)

    def _calculate_pnl(self, position: Position, current_price: float) -> float:
        """Calculate P&L in dollars for a position."""
        price_diff = current_price - position.entry_price
        if position.direction == Direction.SHORT:
            price_diff = -price_diff
        ticks = price_diff / self._tick_size
        return ticks * self._tick_value * position.quantity

    async def _emit_order_update(self, update: OrderUpdate) -> None:
        for cb in self._order_update_callbacks:
            try:
                await cb(update)
            except Exception:
                logger.exception("Error in order update callback")

    async def _set_connection_status(self, status: ConnectionStatus) -> None:
        self._connection_status = status
        for cb in self._connection_callbacks:
            try:
                await cb(status)
            except Exception:
                logger.exception("Error in connection status callback")

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """Reset daily P&L counter (called at session start)."""
        self._daily_pnl = 0.0


class _PaperOrder:
    """Internal tracking for a paper bracket order's SL/TP levels."""

    __slots__ = (
        "entry_order_id", "sl_order_id", "tp_order_id",
        "instrument", "direction", "quantity",
        "fill_price", "sl_price", "tp_price",
    )

    def __init__(
        self,
        entry_order_id: str,
        sl_order_id: str,
        tp_order_id: str,
        instrument: str,
        direction: Direction,
        quantity: int,
        fill_price: float,
        sl_price: float,
        tp_price: float,
    ) -> None:
        self.entry_order_id = entry_order_id
        self.sl_order_id = sl_order_id
        self.tp_order_id = tp_order_id
        self.instrument = instrument
        self.direction = direction
        self.quantity = quantity
        self.fill_price = fill_price
        self.sl_price = sl_price
        self.tp_price = tp_price
