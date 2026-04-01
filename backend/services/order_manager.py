"""
Order Manager — bracket order placement, lifecycle tracking, and phantom tracking.

Responsibilities:
1. Accept a signal → place bracket order via broker → persist TradeRecord.
2. Reject a signal → mark it rejected → create PhantomTradeRecord stub.
3. Listen to OrderUpdate callbacks → close out TradeRecord on SL/TP fill.
4. Track phantom trades: as ticks arrive for rejected signals, determine
   whether TP or SL would have been hit and record the outcome.
5. After each trade close, write an AccountSnapshotRecord.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.broker_client import BrokerClient
from backend.core.models import (
    BracketOrderRequest,
    Direction,
    OrderStatus,
    OrderUpdate,
    Signal,
    SignalDecision,
    Tick,
)
from backend.database.models import (
    AccountSnapshotRecord,
    PhantomTradeRecord,
    SignalRecord,
    TradeRecord,
)

logger = logging.getLogger(__name__)


def _uid() -> str:
    return uuid.uuid4().hex


class _PhantomOrder:
    """Internal state for a rejected signal being phantom-tracked."""

    __slots__ = (
        "signal_id",
        "direction",
        "entry_price",
        "sl_price",
        "tp_price",
        "instrument",
        "tick_value",
        "tick_size",
        "quantity",
        "start_time",
        "max_favorable",
        "max_adverse",
        "resolved",
    )

    def __init__(
        self,
        signal: Signal,
        tick_value: float,
        tick_size: float,
    ) -> None:
        self.signal_id = signal.id
        self.direction = signal.direction
        self.entry_price = signal.entry_price
        self.sl_price = signal.stop_loss_price
        self.tp_price = signal.take_profit_price
        self.instrument = signal.instrument
        self.tick_value = tick_value
        self.tick_size = tick_size
        self.quantity = 1
        self.start_time = datetime.utcnow()
        self.max_favorable: float = 0.0
        self.max_adverse: float = 0.0
        self.resolved = False

    def update_excursion(self, price: float) -> None:
        if self.direction == Direction.LONG:
            favorable = price - self.entry_price
            adverse = self.entry_price - price
        else:
            favorable = self.entry_price - price
            adverse = price - self.entry_price
        self.max_favorable = max(self.max_favorable, favorable)
        self.max_adverse = max(self.max_adverse, adverse)

    def hit_tp(self, price: float) -> bool:
        if self.direction == Direction.LONG:
            return price >= self.tp_price
        return price <= self.tp_price

    def hit_sl(self, price: float) -> bool:
        if self.direction == Direction.LONG:
            return price <= self.sl_price
        return price >= self.sl_price

    def calc_pnl(self, exit_price: float) -> float:
        diff = exit_price - self.entry_price
        if self.direction == Direction.SHORT:
            diff = -diff
        return (diff / self.tick_size) * self.tick_value * self.quantity


class OrderManager:
    """Manages the order lifecycle from signal accept/reject to trade close."""

    def __init__(
        self,
        broker: BrokerClient,
        session_factory: async_sessionmaker[AsyncSession],
        tick_value: float = 12.50,
        tick_size: float = 0.25,
        phase: str = "EVAL",
    ) -> None:
        self._broker = broker
        self._session_factory = session_factory
        self._tick_value = tick_value
        self._tick_size = tick_size
        self._phase = phase

        # Map entry_order_id → signal_id for matching fills
        self._open_orders: dict[str, str] = {}
        # Map signal_id → PhantomOrder for rejected signal tracking
        self._phantom_orders: dict[str, _PhantomOrder] = {}
        # Map entry_order_id → fill_price (to compute P&L on exit)
        self._fill_prices: dict[str, float] = {}

        # Register for order updates from broker
        self._broker.on_order_update(self._on_order_update)

        # Callbacks invoked when a trade closes: (signal_id, pnl)
        self._trade_close_callbacks: list[Any] = []

    # ------------------------------------------------------------------
    # Public callbacks registration
    # ------------------------------------------------------------------

    def on_trade_close(self, callback: Any) -> None:
        """Register a callback invoked when a trade closes: callback(signal_id, pnl)."""
        self._trade_close_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Signal accept / reject
    # ------------------------------------------------------------------

    async def accept_signal(
        self, signal: Signal, quantity: int = 1
    ) -> TradeRecord | None:
        """Place a bracket order for the signal and persist a TradeRecord."""
        request = BracketOrderRequest(
            instrument=signal.instrument,
            direction=signal.direction,
            quantity=quantity,
            entry_price=signal.entry_price,
            stop_loss_price=signal.stop_loss_price,
            take_profit_price=signal.take_profit_price,
        )

        result = await self._broker.place_bracket_order(request)

        now = datetime.utcnow()
        trade_id = _uid()
        trade = TradeRecord(
            id=trade_id,
            signal_id=signal.id,
            order_id=result.entry_order_id,
            fill_price=signal.entry_price,
            fill_timestamp=now,
        )

        # Update the signal decision in the DB
        async with self._session_factory() as sess:
            sig_rec = await sess.get(SignalRecord, signal.id)
            if sig_rec is not None:
                sig_rec.decision = SignalDecision.ACCEPTED.value
                sig_rec.decision_timestamp = now

            sess.add(trade)
            await sess.commit()

        # Track order for fill matching
        self._open_orders[result.entry_order_id] = signal.id
        self._fill_prices[result.entry_order_id] = signal.entry_price

        logger.info(
            "Signal %s accepted → order %s placed",
            signal.id, result.entry_order_id,
        )
        return trade

    async def reject_signal(self, signal: Signal) -> PhantomTradeRecord:
        """Mark signal as rejected and create a PhantomTradeRecord stub."""
        now = datetime.utcnow()
        phantom_id = _uid()
        phantom = PhantomTradeRecord(
            id=phantom_id,
            signal_id=signal.id,
        )

        async with self._session_factory() as sess:
            sig_rec = await sess.get(SignalRecord, signal.id)
            if sig_rec is not None:
                sig_rec.decision = SignalDecision.REJECTED.value
                sig_rec.decision_timestamp = now
            sess.add(phantom)
            await sess.commit()

        # Start phantom tracking
        self._phantom_orders[signal.id] = _PhantomOrder(
            signal=signal,
            tick_value=self._tick_value,
            tick_size=self._tick_size,
        )

        logger.info("Signal %s rejected → phantom tracking started", signal.id)
        return phantom

    # ------------------------------------------------------------------
    # Tick feed for phantom tracking
    # ------------------------------------------------------------------

    async def on_tick(self, tick: Tick) -> None:
        """Feed a tick to phantom tracker — resolves TP/SL outcomes."""
        to_resolve: list[str] = []

        for sig_id, phantom in self._phantom_orders.items():
            if phantom.instrument != tick.instrument or phantom.resolved:
                continue

            phantom.update_excursion(tick.price)

            hit_tp = phantom.hit_tp(tick.price)
            hit_sl = phantom.hit_sl(tick.price)

            if hit_tp or hit_sl:
                exit_price = phantom.tp_price if hit_tp else phantom.sl_price
                pnl = phantom.calc_pnl(exit_price)
                duration = (datetime.utcnow() - phantom.start_time).total_seconds()

                async with self._session_factory() as sess:
                    from sqlalchemy import select as _select
                    result = await sess.execute(
                        _select(PhantomTradeRecord).where(
                            PhantomTradeRecord.signal_id == sig_id
                        )
                    )
                    rec = result.scalar_one_or_none()
                    if rec is not None:
                        rec.would_have_hit_tp = hit_tp
                        rec.would_have_hit_sl = hit_sl
                        rec.phantom_pnl = round(pnl, 2)
                        rec.phantom_duration = round(duration, 1)
                        rec.max_favorable_excursion = round(phantom.max_favorable, 4)
                        rec.max_adverse_excursion = round(phantom.max_adverse, 4)
                        await sess.commit()

                phantom.resolved = True
                to_resolve.append(sig_id)
                logger.info(
                    "Phantom %s resolved: %s @ %.2f (P&L: %.2f)",
                    sig_id, "TP" if hit_tp else "SL", exit_price, pnl,
                )

        for sig_id in to_resolve:
            del self._phantom_orders[sig_id]

    # ------------------------------------------------------------------
    # Order update callback (called by broker)
    # ------------------------------------------------------------------

    async def _on_order_update(self, update: OrderUpdate) -> None:
        """Handle broker order update events to close out TradeRecords."""
        # Only care about fills that are exits (have a parent order id)
        if update.status != OrderStatus.FILLED:
            return
        if not update.parent_order_id:
            return

        entry_id = update.parent_order_id
        signal_id = self._open_orders.get(entry_id)
        if signal_id is None:
            return

        fill_price = self._fill_prices.get(entry_id, update.fill_price)
        exit_price = update.fill_price
        exit_type = "TP" if "tp" in update.order_id else "SL"

        # Compute realised P&L
        direction = update.direction
        price_diff = exit_price - fill_price
        if direction == Direction.SHORT:
            price_diff = -price_diff
        ticks = price_diff / self._tick_size
        pnl = round(ticks * self._tick_value * update.filled_quantity, 2)

        now = datetime.utcnow()

        async with self._session_factory() as sess:
            from sqlalchemy import select as _select

            result = await sess.execute(
                _select(TradeRecord).where(TradeRecord.signal_id == signal_id)
            )
            trade = result.scalar_one_or_none()
            if trade is not None:
                trade.exit_price = exit_price
                trade.exit_timestamp = now
                trade.actual_pnl = pnl
                if trade.fill_timestamp:
                    trade.duration_seconds = (
                        now - trade.fill_timestamp
                    ).total_seconds()

            # Write account snapshot
            account = await self._broker.get_account_info()
            snap = AccountSnapshotRecord(
                id=_uid(),
                timestamp=now,
                balance=account.balance,
                eod_threshold=account.eod_threshold,
                daily_pnl=account.daily_pnl,
                phase=self._phase,
            )
            sess.add(snap)
            await sess.commit()

        # Clean up tracking
        del self._open_orders[entry_id]
        self._fill_prices.pop(entry_id, None)

        logger.info(
            "Trade closed: %s %s @ %.2f (P&L: %.2f)",
            exit_type, signal_id, exit_price, pnl,
        )

        # Notify subscribers (e.g. circuit breaker check)
        for cb in self._trade_close_callbacks:
            try:
                await cb(signal_id, pnl)
            except Exception:
                logger.exception("Error in trade close callback")
