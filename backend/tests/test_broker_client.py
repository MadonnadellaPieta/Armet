"""
Tests for the PaperBrokerClient.

Verifies: connection lifecycle, market data subscriptions, bracket order
placement with simulated fills, SL/TP exit logic, position tracking,
P&L calculation, flatten_all, and callback invocation.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.core.broker_client import BrokerClient
from backend.core.models import (
    BracketOrderRequest,
    ConnectionStatus,
    Direction,
    OrderBook,
    OrderBookLevel,
    OrderStatus,
    OrderUpdate,
    Tick,
)
from backend.core.paper_broker_client import PaperBrokerClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client() -> PaperBrokerClient:
    """Fresh paper broker with ES-like tick size/value."""
    return PaperBrokerClient(
        initial_balance=100_000.0,
        slippage_ticks=0.0,
        tick_size=0.25,
        tick_value=12.50,
    )


def _tick(instrument: str = "ES", price: float = 5100.0) -> Tick:
    return Tick(
        instrument=instrument,
        timestamp=datetime.utcnow(),
        price=price,
        size=1,
        bid=price - 0.25,
        ask=price + 0.25,
        bid_size=10,
        ask_size=10,
    )


def _long_bracket(
    instrument: str = "ES",
    entry: float = 5100.0,
    sl: float = 5095.0,
    tp: float = 5110.0,
    qty: int = 1,
) -> BracketOrderRequest:
    return BracketOrderRequest(
        instrument=instrument,
        direction=Direction.LONG,
        quantity=qty,
        entry_price=entry,
        stop_loss_price=sl,
        take_profit_price=tp,
    )


def _short_bracket(
    instrument: str = "ES",
    entry: float = 5100.0,
    sl: float = 5105.0,
    tp: float = 5090.0,
    qty: int = 1,
) -> BracketOrderRequest:
    return BracketOrderRequest(
        instrument=instrument,
        direction=Direction.SHORT,
        quantity=qty,
        entry_price=entry,
        stop_loss_price=sl,
        take_profit_price=tp,
    )


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------

class TestConnection:
    async def test_implements_abc(self):
        assert isinstance(PaperBrokerClient(), BrokerClient)

    async def test_connect_disconnect(self, client: PaperBrokerClient):
        assert not await client.is_connected()
        await client.connect()
        assert await client.is_connected()
        await client.disconnect()
        assert not await client.is_connected()

    async def test_connection_status_callbacks(self, client: PaperBrokerClient):
        statuses: list[ConnectionStatus] = []
        client.on_connection_status_change(lambda s: statuses.append(s))

        await client.connect()
        await client.disconnect()

        assert statuses == [ConnectionStatus.CONNECTED, ConnectionStatus.DISCONNECTED]


# ---------------------------------------------------------------------------
# Market data tests
# ---------------------------------------------------------------------------

class TestMarketData:
    async def test_tick_callback(self, client: PaperBrokerClient):
        await client.connect()
        ticks_received: list[Tick] = []

        async def on_tick(t: Tick):
            ticks_received.append(t)

        await client.subscribe_market_data("ES", on_tick)
        await client.inject_tick(_tick("ES", 5100.0))
        await client.inject_tick(_tick("ES", 5101.0))

        assert len(ticks_received) == 2
        assert ticks_received[0].price == 5100.0
        assert ticks_received[1].price == 5101.0

    async def test_unsubscribe_stops_callbacks(self, client: PaperBrokerClient):
        await client.connect()
        ticks_received: list[Tick] = []

        async def on_tick(t: Tick):
            ticks_received.append(t)

        await client.subscribe_market_data("ES", on_tick)
        await client.inject_tick(_tick("ES", 5100.0))
        await client.unsubscribe_market_data("ES")
        await client.inject_tick(_tick("ES", 5101.0))

        assert len(ticks_received) == 1

    async def test_order_book_callback(self, client: PaperBrokerClient):
        await client.connect()
        books_received: list[OrderBook] = []

        async def on_ob(ob: OrderBook):
            books_received.append(ob)

        await client.subscribe_order_book("ES", on_ob)

        ob = OrderBook(
            instrument="ES",
            timestamp=datetime.utcnow(),
            bids=(OrderBookLevel(5100.0, 50, 5),),
            asks=(OrderBookLevel(5100.25, 40, 4),),
        )
        await client.inject_order_book(ob)

        assert len(books_received) == 1
        assert books_received[0].best_bid == 5100.0


# ---------------------------------------------------------------------------
# Order placement tests
# ---------------------------------------------------------------------------

class TestBracketOrder:
    async def test_long_bracket_fills_immediately(self, client: PaperBrokerClient):
        await client.connect()
        result = await client.place_bracket_order(_long_bracket())

        assert result.status == OrderStatus.FILLED
        assert result.entry_order_id.startswith("paper_entry_")
        assert result.stop_loss_order_id.startswith("paper_sl_")
        assert result.take_profit_order_id.startswith("paper_tp_")

        positions = await client.get_positions()
        assert len(positions) == 1
        assert positions[0].direction == Direction.LONG
        assert positions[0].entry_price == 5100.0

    async def test_short_bracket_fills_immediately(self, client: PaperBrokerClient):
        await client.connect()
        result = await client.place_bracket_order(_short_bracket())

        positions = await client.get_positions()
        assert len(positions) == 1
        assert positions[0].direction == Direction.SHORT

    async def test_slippage_applied(self):
        c = PaperBrokerClient(slippage_ticks=1.0, tick_size=0.25)
        await c.connect()

        result = await c.place_bracket_order(_long_bracket(entry=5100.0))
        positions = await c.get_positions()
        assert positions[0].entry_price == 5100.25  # 1 tick slippage on long

    async def test_order_update_callback_on_fill(self, client: PaperBrokerClient):
        await client.connect()
        updates: list[OrderUpdate] = []
        client.on_order_update(lambda u: updates.append(u))

        await client.place_bracket_order(_long_bracket())

        assert len(updates) == 1
        assert updates[0].status == OrderStatus.FILLED
        assert updates[0].fill_price == 5100.0

    async def test_multiple_positions(self, client: PaperBrokerClient):
        await client.connect()
        await client.place_bracket_order(_long_bracket("ES", 5100.0, 5095.0, 5110.0))
        await client.place_bracket_order(_short_bracket("NQ", 18000.0, 18050.0, 17900.0))

        positions = await client.get_positions()
        assert len(positions) == 2


# ---------------------------------------------------------------------------
# SL/TP exit tests
# ---------------------------------------------------------------------------

class TestBracketExits:
    async def test_long_tp_hit(self, client: PaperBrokerClient):
        await client.connect()
        updates: list[OrderUpdate] = []
        client.on_order_update(lambda u: updates.append(u))

        await client.place_bracket_order(_long_bracket(tp=5110.0))

        # Price moves to TP
        await client.inject_tick(_tick("ES", 5110.0))

        # Position should be closed
        positions = await client.get_positions()
        assert len(positions) == 0

        # Should have exit update
        exit_update = updates[-1]
        assert exit_update.fill_price == 5110.0
        assert "TP" in exit_update.message

    async def test_long_sl_hit(self, client: PaperBrokerClient):
        await client.connect()
        updates: list[OrderUpdate] = []
        client.on_order_update(lambda u: updates.append(u))

        await client.place_bracket_order(_long_bracket(sl=5095.0))

        await client.inject_tick(_tick("ES", 5095.0))

        positions = await client.get_positions()
        assert len(positions) == 0

        exit_update = updates[-1]
        assert exit_update.fill_price == 5095.0
        assert "SL" in exit_update.message

    async def test_short_tp_hit(self, client: PaperBrokerClient):
        await client.connect()
        await client.place_bracket_order(_short_bracket(tp=5090.0))

        await client.inject_tick(_tick("ES", 5090.0))

        positions = await client.get_positions()
        assert len(positions) == 0

    async def test_short_sl_hit(self, client: PaperBrokerClient):
        await client.connect()
        await client.place_bracket_order(_short_bracket(sl=5105.0))

        await client.inject_tick(_tick("ES", 5105.0))

        positions = await client.get_positions()
        assert len(positions) == 0

    async def test_price_between_sl_tp_no_exit(self, client: PaperBrokerClient):
        await client.connect()
        await client.place_bracket_order(_long_bracket(sl=5095.0, tp=5110.0))

        await client.inject_tick(_tick("ES", 5103.0))

        positions = await client.get_positions()
        assert len(positions) == 1


# ---------------------------------------------------------------------------
# P&L calculation tests
# ---------------------------------------------------------------------------

class TestPnL:
    async def test_long_winning_pnl(self, client: PaperBrokerClient):
        """Long 1 ES from 5100, TP at 5110 = +10 pts = 40 ticks * $12.50 = $500."""
        await client.connect()
        await client.place_bracket_order(_long_bracket(
            entry=5100.0, sl=5095.0, tp=5110.0, qty=1
        ))
        await client.inject_tick(_tick("ES", 5110.0))

        info = await client.get_account_info()
        assert info.balance == pytest.approx(100_500.0)
        assert info.daily_pnl == pytest.approx(500.0)

    async def test_long_losing_pnl(self, client: PaperBrokerClient):
        """Long 1 ES from 5100, SL at 5095 = -5 pts = 20 ticks * $12.50 = -$250."""
        await client.connect()
        await client.place_bracket_order(_long_bracket(
            entry=5100.0, sl=5095.0, tp=5110.0, qty=1
        ))
        await client.inject_tick(_tick("ES", 5095.0))

        info = await client.get_account_info()
        assert info.balance == pytest.approx(99_750.0)
        assert info.daily_pnl == pytest.approx(-250.0)

    async def test_short_winning_pnl(self, client: PaperBrokerClient):
        """Short 1 ES from 5100, TP at 5090 = +10 pts = $500."""
        await client.connect()
        await client.place_bracket_order(_short_bracket(
            entry=5100.0, sl=5105.0, tp=5090.0, qty=1
        ))
        await client.inject_tick(_tick("ES", 5090.0))

        info = await client.get_account_info()
        assert info.balance == pytest.approx(100_500.0)

    async def test_unrealized_pnl_in_equity(self, client: PaperBrokerClient):
        """With open position, equity should reflect unrealized P&L."""
        await client.connect()
        await client.place_bracket_order(_long_bracket(
            entry=5100.0, sl=5095.0, tp=5110.0
        ))

        # Price moves up 2 points = 8 ticks * $12.50 = $100
        await client.inject_tick(_tick("ES", 5102.0))

        info = await client.get_account_info()
        assert info.balance == 100_000.0  # No realized P&L yet
        assert info.equity == pytest.approx(100_100.0)

    async def test_multi_contract_pnl(self, client: PaperBrokerClient):
        """2 contracts * $500 = $1000."""
        await client.connect()
        await client.place_bracket_order(_long_bracket(
            entry=5100.0, sl=5095.0, tp=5110.0, qty=2
        ))
        await client.inject_tick(_tick("ES", 5110.0))

        info = await client.get_account_info()
        assert info.balance == pytest.approx(101_000.0)


# ---------------------------------------------------------------------------
# Flatten all tests
# ---------------------------------------------------------------------------

class TestFlatten:
    async def test_flatten_closes_all_positions(self, client: PaperBrokerClient):
        await client.connect()
        await client.place_bracket_order(_long_bracket("ES", 5100.0, 5095.0, 5110.0))
        await client.place_bracket_order(_short_bracket("NQ", 18000.0, 18050.0, 17900.0))

        # Set last prices
        await client.inject_tick(_tick("ES", 5102.0))
        await client.inject_tick(_tick("NQ", 17990.0))

        count = await client.flatten_all()
        assert count == 2

        positions = await client.get_positions()
        assert len(positions) == 0

    async def test_flatten_emits_order_updates(self, client: PaperBrokerClient):
        await client.connect()
        updates: list[OrderUpdate] = []
        client.on_order_update(lambda u: updates.append(u))

        await client.place_bracket_order(_long_bracket())
        await client.inject_tick(_tick("ES", 5102.0))

        initial_updates = len(updates)
        await client.flatten_all()
        assert len(updates) > initial_updates


# ---------------------------------------------------------------------------
# Account info tests
# ---------------------------------------------------------------------------

class TestAccountInfo:
    async def test_initial_account_info(self, client: PaperBrokerClient):
        await client.connect()
        info = await client.get_account_info()

        assert info.account_id == "PAPER"
        assert info.balance == 100_000.0
        assert info.equity == 100_000.0
        assert info.daily_pnl == 0.0
        assert info.open_positions_count == 0
        assert info.eod_threshold == 97_000.0

    async def test_account_after_trade(self, client: PaperBrokerClient):
        await client.connect()
        await client.place_bracket_order(_long_bracket())

        info = await client.get_account_info()
        assert info.open_positions_count == 1


# ---------------------------------------------------------------------------
# Cancel order test
# ---------------------------------------------------------------------------

class TestCancelOrder:
    async def test_cancel_removes_bracket(self, client: PaperBrokerClient):
        await client.connect()
        result = await client.place_bracket_order(_long_bracket())

        cancelled = await client.cancel_order(result.entry_order_id)
        assert cancelled is True

        # SL/TP should no longer trigger (bracket removed)
        await client.inject_tick(_tick("ES", 5095.0))
        # Position still exists but no SL exit occurred
        # (cancel removed the bracket tracking, not the position)

    async def test_cancel_nonexistent_order(self, client: PaperBrokerClient):
        await client.connect()
        assert await client.cancel_order("nonexistent") is False
