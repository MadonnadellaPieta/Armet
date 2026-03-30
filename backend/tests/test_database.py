"""
CRUD verification tests for all 7 database tables.

Uses an in-memory SQLite database so tests are fast and isolated.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select

from backend.database.models import (
    Base,
    SignalRecord,
    TradeRecord,
    PhantomTradeRecord,
    AccountSnapshotRecord,
    EventRecord,
    CircuitBreakerEventRecord,
    ConfigHistoryRecord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    async_sess = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_sess() as sess:
        yield sess
        await sess.rollback()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _uid() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSignalRecord:
    async def test_insert_and_read(self, session: AsyncSession):
        sig = SignalRecord(
            id=_uid(),
            instrument="ES",
            strategy_name="vwap_reversion",
            direction="LONG",
            entry_price=5100.0,
            sl_price=5095.0,
            tp_price=5110.0,
            rr_ratio=2.0,
            confidence_score=75.0,
            confluence_factors={"order_flow": 0.8, "market_profile": 0.6},
            indicator_state={"vwap": 5098.5, "upper_band": 5105.0},
        )
        session.add(sig)
        await session.commit()

        result = await session.get(SignalRecord, sig.id)
        assert result is not None
        assert result.instrument == "ES"
        assert result.strategy_name == "vwap_reversion"
        assert result.direction == "LONG"
        assert result.entry_price == 5100.0
        assert result.confluence_factors["order_flow"] == 0.8

    async def test_update_decision(self, session: AsyncSession):
        sig = SignalRecord(
            id=_uid(),
            instrument="NQ",
            strategy_name="ema_crossover",
            direction="SHORT",
            entry_price=18000.0,
            sl_price=18050.0,
            tp_price=17900.0,
            rr_ratio=2.0,
            confidence_score=60.0,
        )
        session.add(sig)
        await session.commit()

        sig.decision = "ACCEPTED"
        sig.decision_timestamp = datetime.utcnow()
        await session.commit()

        result = await session.get(SignalRecord, sig.id)
        assert result.decision == "ACCEPTED"
        assert result.decision_timestamp is not None


class TestTradeRecord:
    async def test_insert_with_signal_fk(self, session: AsyncSession):
        sig_id = _uid()
        sig = SignalRecord(
            id=sig_id,
            instrument="MES",
            strategy_name="opening_range",
            direction="LONG",
            entry_price=5100.0,
            sl_price=5095.0,
            tp_price=5110.0,
            rr_ratio=2.0,
            confidence_score=80.0,
        )
        session.add(sig)
        await session.flush()

        trade = TradeRecord(
            id=_uid(),
            signal_id=sig_id,
            order_id="ORD-12345",
            fill_price=5100.25,
            fill_timestamp=datetime.utcnow(),
        )
        session.add(trade)
        await session.commit()

        result = await session.get(TradeRecord, trade.id)
        assert result is not None
        assert result.signal_id == sig_id
        assert result.order_id == "ORD-12345"
        assert result.fill_price == 5100.25
        assert result.was_override is False

    async def test_update_exit(self, session: AsyncSession):
        sig_id = _uid()
        sig = SignalRecord(
            id=sig_id, instrument="ES", strategy_name="vwap_reversion",
            direction="LONG", entry_price=5100.0, sl_price=5095.0,
            tp_price=5110.0, rr_ratio=2.0, confidence_score=70.0,
        )
        trade = TradeRecord(
            id=_uid(), signal_id=sig_id, order_id="ORD-99",
            fill_price=5100.0, fill_timestamp=datetime.utcnow(),
        )
        session.add_all([sig, trade])
        await session.commit()

        trade.exit_price = 5110.0
        trade.exit_timestamp = datetime.utcnow()
        trade.actual_pnl = 500.0
        trade.slippage = 0.25
        trade.duration_seconds = 300.0
        await session.commit()

        result = await session.get(TradeRecord, trade.id)
        assert result.actual_pnl == 500.0
        assert result.duration_seconds == 300.0


class TestPhantomTradeRecord:
    async def test_insert_and_read(self, session: AsyncSession):
        sig_id = _uid()
        sig = SignalRecord(
            id=sig_id, instrument="NQ", strategy_name="ema_crossover",
            direction="SHORT", entry_price=18000.0, sl_price=18050.0,
            tp_price=17900.0, rr_ratio=2.0, confidence_score=55.0,
            decision="REJECTED",
        )
        phantom = PhantomTradeRecord(
            id=_uid(),
            signal_id=sig_id,
            would_have_hit_tp=True,
            would_have_hit_sl=False,
            phantom_pnl=500.0,
            phantom_duration=600.0,
            max_favorable_excursion=550.0,
            max_adverse_excursion=-100.0,
        )
        session.add_all([sig, phantom])
        await session.commit()

        result = await session.get(PhantomTradeRecord, phantom.id)
        assert result.would_have_hit_tp is True
        assert result.phantom_pnl == 500.0
        assert result.max_adverse_excursion == -100.0


class TestAccountSnapshotRecord:
    async def test_insert_and_read(self, session: AsyncSession):
        snap = AccountSnapshotRecord(
            id=_uid(),
            balance=100000.0,
            eod_threshold=97000.0,
            daily_pnl=250.0,
            phase="EVAL",
        )
        session.add(snap)
        await session.commit()

        result = await session.get(AccountSnapshotRecord, snap.id)
        assert result.balance == 100000.0
        assert result.phase == "EVAL"
        assert result.timestamp is not None


class TestEventRecord:
    async def test_insert_and_read(self, session: AsyncSession):
        evt = EventRecord(
            id=_uid(),
            timestamp=datetime(2026, 3, 30, 13, 30),
            name="Non-Farm Payroll",
            impact_level="HIGH",
            forecast="200K",
            previous="185K",
        )
        session.add(evt)
        await session.commit()

        result = await session.get(EventRecord, evt.id)
        assert result.name == "Non-Farm Payroll"
        assert result.impact_level == "HIGH"
        assert result.actual is None  # not yet released


class TestCircuitBreakerEventRecord:
    async def test_insert_and_read(self, session: AsyncSession):
        cb = CircuitBreakerEventRecord(
            id=_uid(),
            trigger_type="MAX_CONSECUTIVE_LOSSES",
            trigger_value="3",
            positions_flattened=2,
        )
        session.add(cb)
        await session.commit()

        result = await session.get(CircuitBreakerEventRecord, cb.id)
        assert result.trigger_type == "MAX_CONSECUTIVE_LOSSES"
        assert result.positions_flattened == 2


class TestConfigHistoryRecord:
    async def test_insert_and_read(self, session: AsyncSession):
        ch = ConfigHistoryRecord(
            id=_uid(),
            parameter_name="strategies.vwap_reversion.sd_multiplier",
            old_value="2.0",
            new_value="2.5",
        )
        session.add(ch)
        await session.commit()

        result = await session.get(ConfigHistoryRecord, ch.id)
        assert result.parameter_name == "strategies.vwap_reversion.sd_multiplier"
        assert result.old_value == "2.0"
        assert result.new_value == "2.5"


class TestRelationships:
    async def test_signal_to_trade_relationship(self, session: AsyncSession):
        sig_id = _uid()
        sig = SignalRecord(
            id=sig_id, instrument="ES", strategy_name="vwap_reversion",
            direction="LONG", entry_price=5100.0, sl_price=5095.0,
            tp_price=5110.0, rr_ratio=2.0, confidence_score=80.0,
            decision="ACCEPTED",
        )
        trade = TradeRecord(
            id=_uid(), signal_id=sig_id, order_id="ORD-REL-1",
            fill_price=5100.0, fill_timestamp=datetime.utcnow(),
        )
        session.add_all([sig, trade])
        await session.commit()

        # Query signal and navigate to trade
        result = await session.execute(
            select(SignalRecord).where(SignalRecord.id == sig_id)
        )
        loaded_sig = result.scalar_one()
        await session.refresh(loaded_sig, ["trade"])
        assert loaded_sig.trade is not None
        assert loaded_sig.trade.order_id == "ORD-REL-1"

    async def test_signal_to_phantom_relationship(self, session: AsyncSession):
        sig_id = _uid()
        sig = SignalRecord(
            id=sig_id, instrument="MNQ", strategy_name="opening_range",
            direction="SHORT", entry_price=18000.0, sl_price=18050.0,
            tp_price=17900.0, rr_ratio=2.0, confidence_score=45.0,
            decision="EXPIRED",
        )
        phantom = PhantomTradeRecord(
            id=_uid(), signal_id=sig_id,
            would_have_hit_sl=True, would_have_hit_tp=False,
            phantom_pnl=-250.0, max_favorable_excursion=50.0,
            max_adverse_excursion=-300.0,
        )
        session.add_all([sig, phantom])
        await session.commit()

        result = await session.execute(
            select(SignalRecord).where(SignalRecord.id == sig_id)
        )
        loaded_sig = result.scalar_one()
        await session.refresh(loaded_sig, ["phantom_trade"])
        assert loaded_sig.phantom_trade is not None
        assert loaded_sig.phantom_trade.phantom_pnl == -250.0


class TestQueryPatterns:
    async def test_filter_signals_by_instrument(self, session: AsyncSession):
        for i in range(3):
            session.add(SignalRecord(
                id=_uid(), instrument="ES", strategy_name="vwap_reversion",
                direction="LONG", entry_price=5100.0 + i, sl_price=5095.0,
                tp_price=5110.0, rr_ratio=2.0, confidence_score=70.0,
            ))
        session.add(SignalRecord(
            id=_uid(), instrument="NQ", strategy_name="ema_crossover",
            direction="SHORT", entry_price=18000.0, sl_price=18050.0,
            tp_price=17900.0, rr_ratio=2.0, confidence_score=60.0,
        ))
        await session.commit()

        result = await session.execute(
            select(SignalRecord).where(SignalRecord.instrument == "ES")
        )
        es_signals = result.scalars().all()
        assert len(es_signals) >= 3
