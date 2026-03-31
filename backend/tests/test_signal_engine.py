"""
Tests for the SignalEngine orchestrator.

Covers:
- Bar fan-out to enabled/disabled strategies
- Instrument filtering (only matching strategies run)
- Full pipeline: filters → scorer → risk manager → callback
- Approved signal triggers callback with correct quantity
- Rejected signal (risk) is stored, not emitted
- Signal enriched with confidence_score and confluence_factors
- on_tick feeds OrderFlowFilter and fans out to tick-aware strategies
- Missing account info discards signal
- DB persistence (approved and rejected)
- Quantity adjustment propagated from risk manager
- Multi-strategy, only one matches instrument
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.models import (
    AccountInfo,
    Bar,
    Direction,
    Phase,
    Position,
    Signal,
    SignalDecision,
    Tick,
)
from backend.filters.market_profile import MarketProfileFilter
from backend.filters.order_flow import OrderFlowFilter
from backend.filters.support_resistance import SupportResistanceFilter
from backend.services.confidence_scorer import ConfidenceScorer
from backend.services.risk_manager import RiskManager
from backend.services.signal_engine import SignalEngine
from backend.strategies.base_strategy import BaseStrategy


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_signal(
    instrument: str = "ES",
    strategy_name: str = "test_strat",
    direction: Direction = Direction.LONG,
    entry: float = 5000.0,
    sl: float = 4990.0,
    tp: float = 5030.0,
    confidence: float = 0.5,
) -> Signal:
    rr = abs(tp - entry) / abs(entry - sl)
    return Signal(
        id=uuid.uuid4().hex,
        instrument=instrument,
        strategy_name=strategy_name,
        direction=direction,
        entry_price=entry,
        stop_loss_price=sl,
        take_profit_price=tp,
        rr_ratio=rr,
        confidence_score=confidence,
    )


def _make_bar(instrument: str = "ES", ts: datetime | None = None) -> Bar:
    return Bar(
        instrument=instrument,
        timestamp=ts or datetime(2026, 3, 31, 14, 0),
        open=5000.0, high=5010.0, low=4995.0, close=5005.0,
        volume=1000,
    )


def _make_tick(instrument: str = "ES") -> Tick:
    return Tick(
        instrument=instrument,
        timestamp=datetime(2026, 3, 31, 14, 0),
        price=5005.0,
        size=10,
        bid=5004.75,
        ask=5005.25,
        bid_size=50,
        ask_size=40,
    )


def _account(
    balance: float = 110_000.0,
    equity: float = 110_000.0,
    daily_pnl: float = 0.0,
    eod_threshold: float = 107_000.0,
) -> AccountInfo:
    return AccountInfo(
        account_id="TEST",
        balance=balance,
        equity=equity,
        daily_pnl=daily_pnl,
        eod_threshold=eod_threshold,
        buying_power=50_000.0,
        open_positions_count=0,
    )


class _StubStrategy(BaseStrategy):
    """Strategy stub: always emits a pre-configured signal, or None."""

    def __init__(
        self,
        name: str,
        instrument: str,
        signal: Signal | None = None,
    ) -> None:
        super().__init__(name, instrument, self.default_params())
        self._signal = signal

    @staticmethod
    def default_params() -> dict:
        return {}

    def on_bar(self, bar: Bar) -> Signal | None:
        return self._signal

    def on_tick(self, tick: Tick) -> Signal | None:
        return self._signal


def _make_engine(
    strategies: list[BaseStrategy] | None = None,
    signal_callback=None,
    session_factory=None,
    requested_quantity: int = 1,
) -> SignalEngine:
    """Return a SignalEngine wired with real (but state-free) components."""
    return SignalEngine(
        strategies=strategies or [],
        order_flow_filter=OrderFlowFilter(),
        market_profile_filter=MarketProfileFilter(),
        support_resistance_filter=SupportResistanceFilter(),
        scorer=ConfidenceScorer(),
        risk_manager=RiskManager(),
        signal_callback=signal_callback,
        session_factory=session_factory,
        requested_quantity=requested_quantity,
    )


# ---------------------------------------------------------------------------
# TestSignalEngine — fan-out and filtering
# ---------------------------------------------------------------------------

class TestSignalEngineFanOut:
    async def test_no_strategies_returns_empty(self):
        engine = _make_engine()
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar())
        assert result == []

    async def test_disabled_strategy_skipped(self):
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        strat.enabled = False
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert result == []

    async def test_wrong_instrument_skipped(self):
        """Strategy on NQ should not fire for an ES bar."""
        sig = _make_signal(instrument="NQ")
        strat = _StubStrategy("s1", "NQ", signal=sig)
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert result == []

    async def test_strategy_returning_none_skipped(self):
        strat = _StubStrategy("s1", "ES", signal=None)
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert result == []

    async def test_multiple_strategies_only_matching_fires(self):
        es_sig = _make_signal(instrument="ES")
        nq_sig = _make_signal(instrument="NQ")
        strat_es = _StubStrategy("es_strat", "ES", signal=es_sig)
        strat_nq = _StubStrategy("nq_strat", "NQ", signal=nq_sig)
        engine = _make_engine(strategies=[strat_es, strat_nq])
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert len(result) == 1
        assert result[0].instrument == "ES"

    async def test_two_strategies_same_instrument_both_fire(self):
        sig1 = _make_signal(strategy_name="strat1")
        sig2 = _make_signal(strategy_name="strat2")
        strat1 = _StubStrategy("strat1", "ES", signal=sig1)
        strat2 = _StubStrategy("strat2", "ES", signal=sig2)
        engine = _make_engine(strategies=[strat1, strat2])
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert len(result) == 2


# ---------------------------------------------------------------------------
# TestSignalEngineApproval — full pipeline happy path
# ---------------------------------------------------------------------------

class TestSignalEngineApproval:
    async def test_approved_signal_returned(self):
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert len(result) == 1
        approved = result[0]
        assert approved.decision == SignalDecision.ACCEPTED
        assert approved.decision_timestamp is not None

    async def test_approved_signal_has_confidence_score(self):
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        approved = result[0]
        # Scorer always returns a float in [0, 1]
        assert 0.0 <= approved.confidence_score <= 1.0

    async def test_approved_signal_has_confluence_factors(self):
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        approved = result[0]
        assert isinstance(approved.confluence_factors, dict)
        # Should contain per-filter blocks
        for key in ("order_flow", "market_profile", "support_resistance"):
            assert key in approved.confluence_factors

    async def test_callback_invoked_on_approval(self):
        callback = AsyncMock()
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat], signal_callback=callback)
        engine.set_account(_account())
        await engine.on_bar(_make_bar("ES"))
        callback.assert_awaited_once()

    async def test_callback_receives_signal_and_quantity(self):
        received = []

        async def cb(signal: Signal, qty: int) -> None:
            received.append((signal, qty))

        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat], signal_callback=cb, requested_quantity=2)
        engine.set_account(_account())
        await engine.on_bar(_make_bar("ES"))
        assert len(received) == 1
        signal_out, qty_out = received[0]
        assert signal_out.decision == SignalDecision.ACCEPTED
        assert qty_out == 2

    async def test_original_signal_id_preserved(self):
        sig = _make_signal()
        original_id = sig.id
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert result[0].id == original_id


# ---------------------------------------------------------------------------
# TestSignalEngineRejection — risk manager blocks signal
# ---------------------------------------------------------------------------

class TestSignalEngineRejection:
    async def test_rejected_signal_not_returned(self):
        # naked order: sl=0 will fail first check
        sig = _make_signal(sl=0.0, tp=5030.0)
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert result == []

    async def test_callback_not_called_on_rejection(self):
        callback = AsyncMock()
        sig = _make_signal(sl=0.0, tp=5030.0)
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat], signal_callback=callback)
        engine.set_account(_account())
        await engine.on_bar(_make_bar("ES"))
        callback.assert_not_awaited()

    async def test_rejected_due_to_rr_below_minimum(self):
        """R:R of 0.5 should be rejected (minimum is 1.5)."""
        sig = _make_signal(entry=5000.0, sl=4990.0, tp=5005.0)  # rr ~0.5
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert result == []

    async def test_rejected_due_to_daily_loss_limit(self):
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat])
        # daily_pnl already at the loss limit
        engine.set_account(_account(daily_pnl=-1500.0))
        result = await engine.on_bar(_make_bar("ES"))
        assert result == []

    async def test_rejected_due_to_max_positions(self):
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        # Fill up the position limit (default 2)
        pos1 = Position(
            instrument="ES", direction=Direction.LONG,
            quantity=1, entry_price=4900.0,
            entry_timestamp=datetime.utcnow(),
        )
        pos2 = Position(
            instrument="NQ", direction=Direction.LONG,
            quantity=1, entry_price=20000.0,
            entry_timestamp=datetime.utcnow(),
        )
        engine.set_positions([pos1, pos2])
        result = await engine.on_bar(_make_bar("ES"))
        assert result == []


# ---------------------------------------------------------------------------
# TestSignalEngineNoAccount — missing account guard
# ---------------------------------------------------------------------------

class TestSignalEngineNoAccount:
    async def test_no_account_discards_signal(self):
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat])
        # Do NOT call set_account
        result = await engine.on_bar(_make_bar("ES"))
        assert result == []

    async def test_no_account_callback_not_called(self):
        callback = AsyncMock()
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat], signal_callback=callback)
        result = await engine.on_bar(_make_bar("ES"))
        assert result == []
        callback.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestSignalEngineTick — on_tick handling
# ---------------------------------------------------------------------------

class TestSignalEngineTick:
    async def test_on_tick_feeds_order_flow_filter(self):
        """OrderFlowFilter should accumulate volume from ticks."""
        of_filter = OrderFlowFilter()
        engine = SignalEngine(
            strategies=[],
            order_flow_filter=of_filter,
            market_profile_filter=MarketProfileFilter(),
            support_resistance_filter=SupportResistanceFilter(),
            scorer=ConfidenceScorer(),
            risk_manager=RiskManager(),
        )
        engine.set_account(_account())
        tick = _make_tick("ES")
        await engine.on_tick(tick)
        total = of_filter._buy_volume + of_filter._sell_volume
        assert total > 0

    async def test_on_tick_skips_disabled_strategy(self):
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        strat.enabled = False
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_tick(_make_tick("ES"))
        assert result == []

    async def test_on_tick_skips_wrong_instrument(self):
        sig = _make_signal(instrument="NQ")
        strat = _StubStrategy("s1", "NQ", signal=sig)
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_tick(_make_tick("ES"))
        assert result == []

    async def test_on_tick_approves_signal_from_tick_strategy(self):
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat])
        engine.set_account(_account())
        result = await engine.on_tick(_make_tick("ES"))
        assert len(result) == 1
        assert result[0].decision == SignalDecision.ACCEPTED


# ---------------------------------------------------------------------------
# TestSignalEngineQuantityAdjustment
# ---------------------------------------------------------------------------

class TestSignalEngineQuantityAdjustment:
    async def test_quantity_reduced_by_risk_manager(self):
        """Request 5 contracts but only 1 slot available → qty reduced to 1."""
        received_qty: list[int] = []

        async def cb(signal: Signal, qty: int) -> None:
            received_qty.append(qty)

        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(
            strategies=[strat],
            signal_callback=cb,
            requested_quantity=5,
        )
        engine.set_account(_account())
        # One open position uses 4 of 8 EVAL slots... actually let's use 7
        open_positions = [
            Position(
                instrument="NQ", direction=Direction.LONG,
                quantity=7, entry_price=20000.0,
                entry_timestamp=datetime.utcnow(),
            )
        ]
        engine.set_positions(open_positions)
        # 8 max − 7 open = 1 available; requesting 5 → reduced to 1
        result = await engine.on_bar(_make_bar("ES"))
        assert len(result) == 1
        assert received_qty == [1]


# ---------------------------------------------------------------------------
# TestSignalEngineDBPersistence — database integration
# ---------------------------------------------------------------------------

class TestSignalEngineDBPersistence:
    async def test_approved_signal_persisted_to_db(self):
        """Approved signals should be written to the SignalRecord table."""
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from sqlalchemy import select
        from backend.database.models import Base, SignalRecord
        from backend.database.db import init_db

        test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat], session_factory=factory)
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert len(result) == 1

        async with factory() as session:
            rows = (await session.execute(select(SignalRecord))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == sig.id
        assert row.decision == SignalDecision.ACCEPTED.value

        await test_engine.dispose()

    async def test_rejected_signal_persisted_to_db(self):
        """Rejected signals should be written to the DB with REJECTED decision."""
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from sqlalchemy import select
        from backend.database.models import Base, SignalRecord

        test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

        # sl=0 → naked order → risk manager rejects
        sig = _make_signal(sl=0.0, tp=5030.0)
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat], session_factory=factory)
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert result == []

        async with factory() as session:
            rows = (await session.execute(select(SignalRecord))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.decision == SignalDecision.REJECTED.value
        # Rejection reason stored in indicator_state
        assert "rejection_reason" in (row.indicator_state or {})

        await test_engine.dispose()

    async def test_no_session_factory_does_not_raise(self):
        """Without a session factory, the engine still works, just no DB writes."""
        sig = _make_signal()
        strat = _StubStrategy("s1", "ES", signal=sig)
        engine = _make_engine(strategies=[strat], session_factory=None)
        engine.set_account(_account())
        result = await engine.on_bar(_make_bar("ES"))
        assert len(result) == 1


# ---------------------------------------------------------------------------
# TestSignalEnginePositionState
# ---------------------------------------------------------------------------

class TestSignalEnginePositionState:
    async def test_set_positions_replaces_list(self):
        """set_positions should replace the internal list, not append."""
        engine = _make_engine()
        pos1 = Position(
            instrument="ES", direction=Direction.LONG,
            quantity=1, entry_price=5000.0,
            entry_timestamp=datetime.utcnow(),
        )
        engine.set_positions([pos1])
        pos2 = Position(
            instrument="NQ", direction=Direction.LONG,
            quantity=1, entry_price=20000.0,
            entry_timestamp=datetime.utcnow(),
        )
        engine.set_positions([pos2])
        # Only pos2 should remain
        assert engine._positions == [pos2]

    async def test_set_account_updates_account(self):
        engine = _make_engine()
        acct1 = _account(balance=110_000.0)
        engine.set_account(acct1)
        assert engine._account is acct1
        acct2 = _account(balance=115_000.0)
        engine.set_account(acct2)
        assert engine._account is acct2
