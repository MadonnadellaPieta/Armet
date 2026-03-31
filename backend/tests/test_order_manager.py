"""
Tests for OrderManager.

Verifies: bracket placement, entry-fill handling (TradeRecord creation),
SL/TP exit handling (P&L computation + TradeRecord update), cancel/reject
cleanup, active_orders() / is_flat() queries, and tick-convention P&L math.

Uses an in-memory SQLite database and a mock broker so tests are fast and
isolated from real broker connectivity.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.broker_client import BrokerClient
from backend.core.models import (
    BracketOrderRequest,
    BracketOrderResult,
    Direction,
    OrderStatus,
    OrderUpdate,
    Signal,
    SignalDecision,
)
from backend.database.models import Base, SignalRecord, TradeRecord
from backend.services.order_manager import OrderManager


# ---------------------------------------------------------------------------
# In-memory database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return uuid.uuid4().hex


def _signal(
    instrument: str = "ES",
    direction: Direction = Direction.LONG,
    entry: float = 5100.0,
    sl: float = 5095.0,
    tp: float = 5110.0,
) -> Signal:
    return Signal(
        id=_uid(),
        instrument=instrument,
        strategy_name="test",
        direction=direction,
        entry_price=entry,
        stop_loss_price=sl,
        take_profit_price=tp,
        rr_ratio=2.0,
        confidence_score=0.75,
        decision=SignalDecision.ACCEPTED,
        decision_timestamp=datetime.utcnow(),
    )


def _bracket_result(
    entry_id: str | None = None,
    sl_id: str | None = None,
    tp_id: str | None = None,
) -> BracketOrderResult:
    return BracketOrderResult(
        entry_order_id=entry_id or f"entry_{_uid()}",
        stop_loss_order_id=sl_id or f"sl_{_uid()}",
        take_profit_order_id=tp_id or f"tp_{_uid()}",
        status=OrderStatus.FILLED,
    )


def _order_update(
    order_id: str,
    status: OrderStatus = OrderStatus.FILLED,
    instrument: str = "ES",
    direction: Direction = Direction.LONG,
    fill_price: float = 5100.0,
    quantity: int = 1,
    message: str = "",
    parent_order_id: str = "",
    timestamp: datetime | None = None,
) -> OrderUpdate:
    return OrderUpdate(
        order_id=order_id,
        status=status,
        instrument=instrument,
        direction=direction,
        quantity=quantity,
        filled_quantity=quantity,
        fill_price=fill_price,
        message=message,
        parent_order_id=parent_order_id,
        timestamp=timestamp or datetime.utcnow(),
    )


def _mock_broker(result: BracketOrderResult | None = None) -> BrokerClient:
    """Return an AsyncMock that behaves like a connected BrokerClient."""
    broker = AsyncMock(spec=BrokerClient)
    broker.place_bracket_order.return_value = result or _bracket_result()
    return broker


async def _seed_signal(session_factory, signal: Signal) -> None:
    """Insert a minimal SignalRecord so FK constraints are satisfied."""
    async with session_factory() as session:
        record = SignalRecord(
            id=signal.id,
            instrument=signal.instrument,
            strategy_name=signal.strategy_name,
            direction=signal.direction.value,
            entry_price=signal.entry_price,
            sl_price=signal.stop_loss_price,
            tp_price=signal.take_profit_price,
            rr_ratio=signal.rr_ratio,
            confidence_score=signal.confidence_score,
            decision=signal.decision.value if signal.decision else None,
            decision_timestamp=signal.decision_timestamp,
        )
        session.add(record)
        await session.commit()


# ---------------------------------------------------------------------------
# TestPlaceBracket
# ---------------------------------------------------------------------------


class TestPlaceBracket:
    async def test_calls_broker_place_bracket_order(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal()

        await om.place_bracket(sig, quantity=1)

        broker.place_bracket_order.assert_called_once()
        call_arg: BracketOrderRequest = broker.place_bracket_order.call_args[0][0]
        assert call_arg.instrument == sig.instrument
        assert call_arg.direction == sig.direction
        assert call_arg.quantity == 1
        assert call_arg.entry_price == sig.entry_price
        assert call_arg.stop_loss_price == sig.stop_loss_price
        assert call_arg.take_profit_price == sig.take_profit_price

    async def test_registers_bracket_in_active_orders(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal()

        await om.place_bracket(sig, quantity=2)

        orders = om.active_orders()
        assert result.entry_order_id in orders
        assert orders[result.entry_order_id][0] is sig
        assert orders[result.entry_order_id][1] is result

    async def test_not_flat_after_placement(self, session_factory):
        broker = _mock_broker()
        om = OrderManager(broker, session_factory)

        assert om.is_flat()
        await om.place_bracket(_signal(), quantity=1)
        assert not om.is_flat()

    async def test_multiple_brackets_all_registered(self, session_factory):
        r1 = _bracket_result()
        r2 = _bracket_result()
        broker = AsyncMock(spec=BrokerClient)
        broker.place_bracket_order.side_effect = [r1, r2]
        om = OrderManager(broker, session_factory)

        await om.place_bracket(_signal("ES"), quantity=1)
        await om.place_bracket(_signal("NQ"), quantity=1)

        orders = om.active_orders()
        assert len(orders) == 2
        assert r1.entry_order_id in orders
        assert r2.entry_order_id in orders

    async def test_returns_broker_result(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)

        returned = await om.place_bracket(_signal(), quantity=1)
        assert returned is result


# ---------------------------------------------------------------------------
# TestEntryFill
# ---------------------------------------------------------------------------


class TestEntryFill:
    async def test_entry_fill_stores_fill_price(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal()
        await om.place_bracket(sig, quantity=1)

        bracket = om._orders[result.entry_order_id]
        assert bracket.fill_price is None

        update = _order_update(result.entry_order_id, fill_price=5100.25)
        await om.on_order_update(update)

        assert bracket.fill_price == 5100.25

    async def test_entry_fill_creates_trade_record(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal()
        await _seed_signal(session_factory, sig)
        await om.place_bracket(sig, quantity=1)

        now = datetime.utcnow()
        update = _order_update(result.entry_order_id, fill_price=5100.0, timestamp=now)
        await om.on_order_update(update)

        async with session_factory() as session:
            result_row = await session.execute(
                select(TradeRecord).where(TradeRecord.signal_id == sig.id)
            )
            trade = result_row.scalar_one()

        assert trade.fill_price == 5100.0
        assert trade.fill_timestamp == now
        assert trade.order_id == result.entry_order_id
        assert trade.slippage == pytest.approx(0.0)

    async def test_entry_fill_records_slippage(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal(entry=5100.0)
        await _seed_signal(session_factory, sig)
        await om.place_bracket(sig, quantity=1)

        # Fill 1 tick away (0.25 slippage)
        update = _order_update(result.entry_order_id, fill_price=5100.25)
        await om.on_order_update(update)

        async with session_factory() as session:
            result_row = await session.execute(
                select(TradeRecord).where(TradeRecord.signal_id == sig.id)
            )
            trade = result_row.scalar_one()

        assert trade.slippage == pytest.approx(0.25)

    async def test_entry_fill_trade_record_exit_fields_null(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal()
        await _seed_signal(session_factory, sig)
        await om.place_bracket(sig, quantity=1)

        update = _order_update(result.entry_order_id)
        await om.on_order_update(update)

        async with session_factory() as session:
            result_row = await session.execute(
                select(TradeRecord).where(TradeRecord.signal_id == sig.id)
            )
            trade = result_row.scalar_one()

        assert trade.exit_price is None
        assert trade.actual_pnl is None
        assert trade.duration_seconds is None

    async def test_entry_fill_bracket_stays_active(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal()
        await om.place_bracket(sig, quantity=1)

        update = _order_update(result.entry_order_id, fill_price=5100.0)
        await om.on_order_update(update)

        # Bracket should still be active until exit leg fires
        assert not om.is_flat()
        assert result.entry_order_id in om.active_orders()


# ---------------------------------------------------------------------------
# TestTPHit
# ---------------------------------------------------------------------------


class TestTPHit:
    async def test_tp_hit_removes_from_active_orders(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal(entry=5100.0, tp=5110.0)
        await om.place_bracket(sig, quantity=1)

        # Entry fills
        await om.on_order_update(_order_update(result.entry_order_id, fill_price=5100.0))
        assert not om.is_flat()

        # TP hits
        await om.on_order_update(_order_update(result.take_profit_order_id, fill_price=5110.0))
        assert om.is_flat()

    async def test_tp_hit_updates_trade_record(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal(entry=5100.0, tp=5110.0)
        await _seed_signal(session_factory, sig)
        await om.place_bracket(sig, quantity=1)

        entry_time = datetime.utcnow()
        await om.on_order_update(
            _order_update(result.entry_order_id, fill_price=5100.0, timestamp=entry_time)
        )

        exit_time = entry_time + timedelta(seconds=30)
        await om.on_order_update(
            _order_update(result.take_profit_order_id, fill_price=5110.0, timestamp=exit_time)
        )

        async with session_factory() as session:
            result_row = await session.execute(
                select(TradeRecord).where(TradeRecord.signal_id == sig.id)
            )
            trade = result_row.scalar_one()

        # 10 pts = 40 ticks * $12.50 = $500
        assert trade.exit_price == 5110.0
        assert trade.actual_pnl == pytest.approx(500.0)
        assert trade.duration_seconds == pytest.approx(30.0)

    async def test_tp_hit_short_winning_pnl(self, session_factory):
        """Short 1 ES from 5100 to TP 5090 = +10 pts = $500."""
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal(direction=Direction.SHORT, entry=5100.0, sl=5105.0, tp=5090.0)
        await _seed_signal(session_factory, sig)
        await om.place_bracket(sig, quantity=1)

        await om.on_order_update(_order_update(result.entry_order_id, fill_price=5100.0))
        await om.on_order_update(
            _order_update(result.take_profit_order_id, fill_price=5090.0, direction=Direction.SHORT)
        )

        async with session_factory() as session:
            result_row = await session.execute(
                select(TradeRecord).where(TradeRecord.signal_id == sig.id)
            )
            trade = result_row.scalar_one()

        assert trade.actual_pnl == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# TestSLHit
# ---------------------------------------------------------------------------


class TestSLHit:
    async def test_sl_hit_removes_from_active_orders(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal(entry=5100.0, sl=5095.0)
        await om.place_bracket(sig, quantity=1)

        await om.on_order_update(_order_update(result.entry_order_id, fill_price=5100.0))
        await om.on_order_update(_order_update(result.stop_loss_order_id, fill_price=5095.0))

        assert om.is_flat()

    async def test_sl_hit_records_negative_pnl(self, session_factory):
        """Long 1 ES from 5100 to SL 5095 = -5 pts = -$250."""
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal(entry=5100.0, sl=5095.0, tp=5110.0)
        await _seed_signal(session_factory, sig)
        await om.place_bracket(sig, quantity=1)

        await om.on_order_update(_order_update(result.entry_order_id, fill_price=5100.0))
        await om.on_order_update(_order_update(result.stop_loss_order_id, fill_price=5095.0))

        async with session_factory() as session:
            result_row = await session.execute(
                select(TradeRecord).where(TradeRecord.signal_id == sig.id)
            )
            trade = result_row.scalar_one()

        assert trade.actual_pnl == pytest.approx(-250.0)

    async def test_sl_hit_short_losing_pnl(self, session_factory):
        """Short 1 ES from 5100, SL at 5105 = -5 pts = -$250."""
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal(direction=Direction.SHORT, entry=5100.0, sl=5105.0, tp=5090.0)
        await _seed_signal(session_factory, sig)
        await om.place_bracket(sig, quantity=1)

        await om.on_order_update(
            _order_update(result.entry_order_id, fill_price=5100.0, direction=Direction.SHORT)
        )
        await om.on_order_update(
            _order_update(result.stop_loss_order_id, fill_price=5105.0, direction=Direction.SHORT)
        )

        async with session_factory() as session:
            result_row = await session.execute(
                select(TradeRecord).where(TradeRecord.signal_id == sig.id)
            )
            trade = result_row.scalar_one()

        assert trade.actual_pnl == pytest.approx(-250.0)

    async def test_sl_hit_multi_contract(self, session_factory):
        """Long 3 ES from 5100 to SL 5095 = -5 pts * 3 = -$750."""
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        sig = _signal(entry=5100.0, sl=5095.0, tp=5110.0)
        await _seed_signal(session_factory, sig)
        await om.place_bracket(sig, quantity=3)

        await om.on_order_update(_order_update(result.entry_order_id, fill_price=5100.0))
        await om.on_order_update(_order_update(result.stop_loss_order_id, fill_price=5095.0))

        async with session_factory() as session:
            result_row = await session.execute(
                select(TradeRecord).where(TradeRecord.signal_id == sig.id)
            )
            trade = result_row.scalar_one()

        assert trade.actual_pnl == pytest.approx(-750.0)


# ---------------------------------------------------------------------------
# TestCancelReject
# ---------------------------------------------------------------------------


class TestCancelReject:
    async def test_entry_cancel_removes_bracket(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        await om.place_bracket(_signal(), quantity=1)

        assert not om.is_flat()
        cancel = _order_update(result.entry_order_id, status=OrderStatus.CANCELLED)
        await om.on_order_update(cancel)

        assert om.is_flat()

    async def test_entry_reject_removes_bracket(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        await om.place_bracket(_signal(), quantity=1)

        reject = _order_update(result.entry_order_id, status=OrderStatus.REJECTED, message="Margin")
        await om.on_order_update(reject)

        assert om.is_flat()

    async def test_exit_cancel_removes_bracket(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        await om.place_bracket(_signal(), quantity=1)

        await om.on_order_update(_order_update(result.entry_order_id, fill_price=5100.0))
        cancel = _order_update(result.stop_loss_order_id, status=OrderStatus.CANCELLED)
        await om.on_order_update(cancel)

        assert om.is_flat()

    async def test_unknown_order_id_is_silently_ignored(self, session_factory):
        broker = _mock_broker()
        om = OrderManager(broker, session_factory)

        # Should not raise
        unknown = _order_update("nonexistent_order_id", status=OrderStatus.FILLED)
        await om.on_order_update(unknown)

        assert om.is_flat()


# ---------------------------------------------------------------------------
# TestActiveOrders
# ---------------------------------------------------------------------------


class TestActiveOrders:
    async def test_active_orders_empty_initially(self, session_factory):
        om = OrderManager(_mock_broker(), session_factory)
        assert om.active_orders() == {}

    async def test_active_orders_contains_placed_brackets(self, session_factory):
        r1 = _bracket_result()
        r2 = _bracket_result()
        broker = AsyncMock(spec=BrokerClient)
        broker.place_bracket_order.side_effect = [r1, r2]
        om = OrderManager(broker, session_factory)

        s1 = _signal("ES")
        s2 = _signal("NQ")
        await om.place_bracket(s1, quantity=1)
        await om.place_bracket(s2, quantity=1)

        orders = om.active_orders()
        assert len(orders) == 2

    async def test_active_orders_cleared_after_exit(self, session_factory):
        result = _bracket_result()
        broker = _mock_broker(result)
        om = OrderManager(broker, session_factory)
        await om.place_bracket(_signal(), quantity=1)

        await om.on_order_update(_order_update(result.entry_order_id, fill_price=5100.0))
        await om.on_order_update(_order_update(result.take_profit_order_id, fill_price=5110.0))

        assert om.active_orders() == {}

    async def test_is_flat_true_after_all_exits(self, session_factory):
        r1 = _bracket_result()
        r2 = _bracket_result()
        broker = AsyncMock(spec=BrokerClient)
        broker.place_bracket_order.side_effect = [r1, r2]
        om = OrderManager(broker, session_factory)

        await om.place_bracket(_signal("ES"), quantity=1)
        await om.place_bracket(_signal("NQ"), quantity=1)
        assert not om.is_flat()

        # Close first
        await om.on_order_update(_order_update(r1.entry_order_id, fill_price=5100.0))
        await om.on_order_update(_order_update(r1.stop_loss_order_id, fill_price=5095.0))
        assert not om.is_flat()

        # Close second
        await om.on_order_update(_order_update(r2.entry_order_id, fill_price=18000.0))
        await om.on_order_update(
            _order_update(r2.take_profit_order_id, fill_price=17900.0, instrument="NQ")
        )
        assert om.is_flat()


# ---------------------------------------------------------------------------
# TestPnLCalculation — tick convention math
# ---------------------------------------------------------------------------


class TestPnLCalculation:
    """Verify _compute_pnl uses the correct tick size and value per instrument."""

    def _om(self, session_factory) -> OrderManager:
        return OrderManager(_mock_broker(), session_factory)

    async def test_es_long_winning(self, session_factory):
        om = self._om(session_factory)
        # 10 pts = 40 ticks * $12.50 = $500
        pnl = om._compute_pnl(Direction.LONG, 5100.0, 5110.0, 1, "ES")
        assert pnl == pytest.approx(500.0)

    async def test_es_long_losing(self, session_factory):
        om = self._om(session_factory)
        # -5 pts = -20 ticks * $12.50 = -$250
        pnl = om._compute_pnl(Direction.LONG, 5100.0, 5095.0, 1, "ES")
        assert pnl == pytest.approx(-250.0)

    async def test_es_short_winning(self, session_factory):
        om = self._om(session_factory)
        # Short 5100 → 5090 = +10 pts = $500
        pnl = om._compute_pnl(Direction.SHORT, 5100.0, 5090.0, 1, "ES")
        assert pnl == pytest.approx(500.0)

    async def test_es_short_losing(self, session_factory):
        om = self._om(session_factory)
        # Short 5100 → 5105 = -5 pts = -$250
        pnl = om._compute_pnl(Direction.SHORT, 5100.0, 5105.0, 1, "ES")
        assert pnl == pytest.approx(-250.0)

    async def test_mes_tick_value(self, session_factory):
        om = self._om(session_factory)
        # MES: tick_value = $1.25; 10 pts = 40 ticks * $1.25 = $50
        pnl = om._compute_pnl(Direction.LONG, 5100.0, 5110.0, 1, "MES")
        assert pnl == pytest.approx(50.0)

    async def test_nq_tick_value(self, session_factory):
        om = self._om(session_factory)
        # NQ: tick_value = $5.00; 10 pts = 40 ticks * $5.00 = $200
        pnl = om._compute_pnl(Direction.LONG, 18000.0, 18010.0, 1, "NQ")
        assert pnl == pytest.approx(200.0)

    async def test_mnq_tick_value(self, session_factory):
        om = self._om(session_factory)
        # MNQ: tick_value = $0.50; 10 pts = 40 ticks * $0.50 = $20
        pnl = om._compute_pnl(Direction.LONG, 18000.0, 18010.0, 1, "MNQ")
        assert pnl == pytest.approx(20.0)

    async def test_multi_contract_scales_linearly(self, session_factory):
        om = self._om(session_factory)
        # 2 ES contracts: same 10 pts = $1000
        pnl = om._compute_pnl(Direction.LONG, 5100.0, 5110.0, 2, "ES")
        assert pnl == pytest.approx(1000.0)

    async def test_unknown_instrument_falls_back_to_default(self, session_factory):
        om = self._om(session_factory)
        # Default is ES conventions; 10 pts = $500
        pnl = om._compute_pnl(Direction.LONG, 5100.0, 5110.0, 1, "UNKNOWN")
        assert pnl == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# TestPaperBrokerIntegration — end-to-end with real PaperBrokerClient
# ---------------------------------------------------------------------------


class TestPaperBrokerIntegration:
    """Smoke-test the OrderManager wired to a real PaperBrokerClient."""

    async def test_full_lifecycle_long_tp(self, session_factory):
        from datetime import datetime

        from backend.core.models import Tick
        from backend.core.paper_broker_client import PaperBrokerClient

        broker = PaperBrokerClient(initial_balance=100_000.0, tick_size=0.25, tick_value=12.50)
        await broker.connect()

        om = OrderManager(broker, session_factory)
        broker.on_order_update(om.on_order_update)

        sig = _signal(entry=5100.0, sl=5095.0, tp=5110.0)
        await _seed_signal(session_factory, sig)
        result = await om.place_bracket(sig, quantity=1)

        # PaperBrokerClient immediately fills entry — on_order_update fires
        assert not om.is_flat()

        # Inject tick to trigger TP
        tick = Tick(
            instrument="ES",
            timestamp=datetime.utcnow(),
            price=5110.0,
            size=1,
            bid=5109.75,
            ask=5110.25,
            bid_size=10,
            ask_size=10,
        )
        await broker.inject_tick(tick)

        # Trade should be closed
        assert om.is_flat()

    async def test_full_lifecycle_long_sl(self, session_factory):
        from datetime import datetime

        from backend.core.models import Tick
        from backend.core.paper_broker_client import PaperBrokerClient

        broker = PaperBrokerClient(initial_balance=100_000.0, tick_size=0.25, tick_value=12.50)
        await broker.connect()

        om = OrderManager(broker, session_factory)
        broker.on_order_update(om.on_order_update)

        sig = _signal(entry=5100.0, sl=5095.0, tp=5110.0)
        await _seed_signal(session_factory, sig)
        await om.place_bracket(sig, quantity=1)

        tick = Tick(
            instrument="ES",
            timestamp=datetime.utcnow(),
            price=5095.0,
            size=1,
            bid=5094.75,
            ask=5095.25,
            bid_size=10,
            ask_size=10,
        )
        await broker.inject_tick(tick)

        assert om.is_flat()
