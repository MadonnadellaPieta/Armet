"""
Integration tests — full paper-mode end-to-end pipeline.

Each test uses an in-memory SQLite DB and a PaperBrokerClient.
State is isolated between tests via the conftest fixtures.

Tests
-----
1. Signal pipeline: EMA strategy → filters → confidence → risk → SignalRecord
2. Order placement: accept signal → TradeRecord OPEN
3. SL/TP fill: inject TP tick → TradeRecord closed + positive P&L + AccountSnapshotRecord
4. Rejection / phantom tracking: reject signal → PhantomTradeRecord → TP tracking
5. Circuit breaker trip: losses exceed limit → is_tripped=True + CircuitBreakerEventRecord
6. CB reset + session boundary: reset CB, outside hours → signals suppressed; daily reset
7. Risk guardrails: R:R < 1.5 signal blocked → EventRecord logged, no TradeRecord
"""

from __future__ import annotations

import uuid
from datetime import datetime, time

import pytest
from sqlalchemy import select

from backend.core.models import (
    Bar,
    Direction,
    Signal,
    Tick,
)
from backend.database.models import (
    AccountSnapshotRecord,
    CircuitBreakerEventRecord,
    EventRecord,
    PhantomTradeRecord,
    SignalRecord,
    TradeRecord,
)
from backend.services.circuit_breaker import CircuitBreaker
from backend.services.confidence_scorer import ConfidenceScorer
from backend.services.order_manager import OrderManager
from backend.services.risk_manager import RiskManager
from backend.services.session_manager import SessionManager
from backend.services.signal_engine import ApprovedSignal, SignalEngine
from backend.strategies.base_strategy import BaseStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return uuid.uuid4().hex


def _bar(
    close: float,
    volume: int,
    instrument: str = "ES",
    timestamp: datetime | None = None,
) -> Bar:
    ts = timestamp or datetime.utcnow()
    return Bar(
        instrument=instrument,
        timestamp=ts,
        open=close,
        high=close + 0.25,
        low=close - 0.25,
        close=close,
        volume=volume,
    )


def _tick(
    price: float,
    instrument: str = "ES",
    timestamp: datetime | None = None,
) -> Tick:
    ts = timestamp or datetime.utcnow()
    return Tick(
        instrument=instrument,
        timestamp=ts,
        price=price,
        size=10,
        bid=price - 0.25,
        ask=price + 0.25,
        bid_size=100,
        ask_size=100,
    )


def _make_signal(
    direction: Direction = Direction.LONG,
    entry: float = 5100.0,
    sl: float = 5095.0,
    tp: float = 5110.0,
    strategy_name: str = "test",
) -> Signal:
    """Build a well-formed Signal with valid R:R (2.0)."""
    rr = round(abs(tp - entry) / max(abs(entry - sl), 0.01), 2)
    return Signal(
        id=_uid(),
        instrument="ES",
        strategy_name=strategy_name,
        direction=direction,
        entry_price=entry,
        stop_loss_price=sl,
        take_profit_price=tp,
        rr_ratio=rr,
        confidence_score=0.6,
        confluence_factors={},
        indicator_state={},
    )


async def _persist_signal(session_factory, signal: Signal) -> None:
    """Persist a SignalRecord so OrderManager can update its decision."""
    async with session_factory() as sess:
        rec = SignalRecord(
            id=signal.id,
            instrument=signal.instrument,
            strategy_name=signal.strategy_name,
            direction=signal.direction.value,
            entry_price=signal.entry_price,
            sl_price=signal.stop_loss_price,
            tp_price=signal.take_profit_price,
            rr_ratio=signal.rr_ratio,
            confidence_score=signal.confidence_score,
            confluence_factors=signal.confluence_factors,
            indicator_state=signal.indicator_state,
        )
        sess.add(rec)
        await sess.commit()


class _StaticStrategy(BaseStrategy):
    """One-shot strategy that emits a pre-configured signal once."""

    def __init__(self, signal_to_emit: Signal) -> None:
        super().__init__("static_test", signal_to_emit.instrument, {})
        self._signal = signal_to_emit
        self._emitted = False

    @staticmethod
    def default_params() -> dict:
        return {}

    def on_bar(self, bar: Bar) -> Signal | None:
        if not self._emitted:
            self._emitted = True
            return self._signal
        return None


# ---------------------------------------------------------------------------
# Test 1 — Signal pipeline
# ---------------------------------------------------------------------------


class TestSignalPipeline:
    """EMA crossover strategy feeds bars → signal approved → SignalRecord persisted."""

    async def test_ema_crossover_emits_approved_signal(
        self, db_session_factory, paper_broker
    ):
        from backend.strategies.ema_crossover import EMACrossoverStrategy

        strategy = EMACrossoverStrategy("ES")
        engine = SignalEngine(
            strategies=[strategy],
            broker=paper_broker,
            session_factory=db_session_factory,
        )

        # Feed 21 flat bars (both EMAs converge to 5090) then one sharp jump.
        # The big jump causes the fast EMA to cross above the slow EMA.
        approved: list[ApprovedSignal] = []
        for i in range(22):
            close = 5090.0 if i < 21 else 5150.0
            volume = 1000 if i < 21 else 5000
            result = await engine.on_bar(_bar(close, volume))
            approved.extend(result)

        # At least one approved signal must come out
        assert len(approved) >= 1, "Expected at least one approved signal from EMA crossover"

        sig = approved[0].signal
        assert sig.instrument == "ES"
        assert sig.strategy_name == "ema_crossover"
        assert sig.direction == Direction.LONG
        assert sig.rr_ratio >= 1.5
        assert 0.0 <= sig.confidence_score <= 1.0
        assert approved[0].approved_quantity >= 1

        # Verify persisted to DB
        async with db_session_factory() as sess:
            result = await sess.execute(
                select(SignalRecord).where(SignalRecord.instrument == "ES")
            )
            records = result.scalars().all()
        assert len(records) >= 1
        db_sig = records[0]
        assert db_sig.strategy_name == "ema_crossover"
        assert db_sig.rr_ratio >= 1.5


# ---------------------------------------------------------------------------
# Test 2 — Order placement
# ---------------------------------------------------------------------------


class TestOrderPlacement:
    """Accepting a signal places a bracket order and creates an OPEN TradeRecord."""

    async def test_accept_signal_creates_trade_record(
        self, db_session_factory, paper_broker
    ):
        signal = _make_signal(entry=5100.0, sl=5095.0, tp=5110.0)
        await _persist_signal(db_session_factory, signal)

        order_mgr = OrderManager(
            broker=paper_broker,
            session_factory=db_session_factory,
        )
        trade = await order_mgr.accept_signal(signal, quantity=1)

        assert trade is not None
        assert trade.signal_id == signal.id
        assert trade.order_id.startswith("paper_entry_")
        assert trade.fill_price == signal.entry_price
        assert trade.fill_timestamp is not None
        # Trade is OPEN: exit fields are null
        assert trade.exit_price is None
        assert trade.exit_timestamp is None
        assert trade.actual_pnl is None

        # Confirm in DB
        async with db_session_factory() as sess:
            result = await sess.execute(
                select(TradeRecord).where(TradeRecord.signal_id == signal.id)
            )
            db_trade = result.scalar_one_or_none()
        assert db_trade is not None
        assert db_trade.fill_price == 5100.0
        assert db_trade.exit_price is None  # still open

        # Signal decision updated to ACCEPTED
        async with db_session_factory() as sess:
            sig_rec = await sess.get(SignalRecord, signal.id)
        assert sig_rec.decision == "ACCEPTED"


# ---------------------------------------------------------------------------
# Test 3 — SL/TP fill
# ---------------------------------------------------------------------------


class TestTPFill:
    """Injecting a tick at TP price closes the trade with positive P&L
    and writes an AccountSnapshotRecord."""

    async def test_tp_hit_closes_trade_and_writes_snapshot(
        self, db_session_factory, paper_broker
    ):
        # entry=5100, SL=5095, TP=5110 → 10 ticks profit = 10 * $12.50 = $125
        signal = _make_signal(entry=5100.0, sl=5095.0, tp=5110.0)
        await _persist_signal(db_session_factory, signal)

        order_mgr = OrderManager(
            broker=paper_broker,
            session_factory=db_session_factory,
        )
        trade = await order_mgr.accept_signal(signal, quantity=1)
        assert trade is not None

        # Inject tick at TP price
        await paper_broker.inject_tick(_tick(5110.0))

        # Trade should now be closed
        async with db_session_factory() as sess:
            result = await sess.execute(
                select(TradeRecord).where(TradeRecord.signal_id == signal.id)
            )
            db_trade = result.scalar_one()

        assert db_trade.exit_price == 5110.0
        assert db_trade.exit_timestamp is not None
        assert db_trade.actual_pnl is not None
        assert db_trade.actual_pnl > 0, f"Expected positive P&L, got {db_trade.actual_pnl}"

        # AccountSnapshotRecord must be written
        async with db_session_factory() as sess:
            result = await sess.execute(select(AccountSnapshotRecord))
            snapshots = result.scalars().all()
        assert len(snapshots) >= 1
        snap = snapshots[-1]
        assert snap.balance > 0
        assert snap.phase == "EVAL"

        # Verify broker balance increased
        account = await paper_broker.get_account_info()
        assert account.balance > 110_000.0  # profit added


# ---------------------------------------------------------------------------
# Test 4 — Rejection / phantom tracking
# ---------------------------------------------------------------------------


class TestRejectionPhantomTracking:
    """Rejecting a signal creates a PhantomTradeRecord. Feeding the TP price
    resolves it with would_have_hit_tp=True."""

    async def test_rejection_creates_phantom_and_tracks_outcome(
        self, db_session_factory, paper_broker
    ):
        signal = _make_signal(entry=5100.0, sl=5095.0, tp=5110.0)
        await _persist_signal(db_session_factory, signal)

        order_mgr = OrderManager(
            broker=paper_broker,
            session_factory=db_session_factory,
        )
        phantom = await order_mgr.reject_signal(signal)

        # PhantomTradeRecord stub created
        assert phantom is not None
        assert phantom.signal_id == signal.id
        assert phantom.would_have_hit_tp is None  # not resolved yet

        # Signal decision updated to REJECTED
        async with db_session_factory() as sess:
            sig_rec = await sess.get(SignalRecord, signal.id)
        assert sig_rec.decision == "REJECTED"

        # No TradeRecord should exist
        async with db_session_factory() as sess:
            result = await sess.execute(
                select(TradeRecord).where(TradeRecord.signal_id == signal.id)
            )
            assert result.scalar_one_or_none() is None

        # Feed a tick that crosses TP
        await order_mgr.on_tick(_tick(5110.0))

        # PhantomTradeRecord resolved
        async with db_session_factory() as sess:
            result = await sess.execute(
                select(PhantomTradeRecord).where(
                    PhantomTradeRecord.signal_id == signal.id
                )
            )
            db_phantom = result.scalar_one()

        assert db_phantom.would_have_hit_tp is True
        assert db_phantom.would_have_hit_sl is False
        assert db_phantom.phantom_pnl is not None
        assert db_phantom.phantom_pnl > 0
        assert db_phantom.phantom_duration is not None


# ---------------------------------------------------------------------------
# Test 5 — Circuit breaker trip
# ---------------------------------------------------------------------------


class TestCircuitBreakerTrip:
    """Losses exceeding the daily limit trip the circuit breaker, prevent new
    signals, and persist a CircuitBreakerEventRecord."""

    async def test_daily_loss_trips_circuit_breaker(
        self, db_session_factory, paper_broker
    ):
        # Use a low threshold so we trip quickly: 3 trades × $62.50 = $187.50 loss
        cb = CircuitBreaker(db_session_factory, {"max_daily_loss": 180.0})

        order_mgr = OrderManager(
            broker=paper_broker,
            session_factory=db_session_factory,
        )

        # Wire circuit breaker: after each close, check daily loss
        async def _check_cb(signal_id: str, pnl: float) -> None:
            account = await paper_broker.get_account_info()
            await cb.check_daily_loss(account.daily_pnl)

        order_mgr.on_trade_close(_check_cb)

        # Place 3 trades, all hitting SL at 5 ticks below entry ($62.50 each)
        for _ in range(3):
            sig = _make_signal(entry=5100.0, sl=5095.0, tp=5110.0)
            await _persist_signal(db_session_factory, sig)
            await order_mgr.accept_signal(sig, quantity=1)
            # Hit SL
            await paper_broker.inject_tick(_tick(5095.0))
            # Re-open next trade price (broker positions cleared)

        assert cb.is_tripped, "Circuit breaker should be tripped after 3 losses"
        assert cb.trip_reason is not None

        # CircuitBreakerEventRecord must exist
        async with db_session_factory() as sess:
            result = await sess.execute(select(CircuitBreakerEventRecord))
            events = result.scalars().all()
        assert len(events) >= 1
        event = events[0]
        assert event.trigger_type == "MAX_DAILY_LOSS"

        # Signal engine should block new signals while tripped
        engine = SignalEngine(
            strategies=[_StaticStrategy(_make_signal())],
            broker=paper_broker,
            session_factory=db_session_factory,
            circuit_breaker=cb,
        )
        new_signals = await engine.on_bar(_bar(5100.0, 1000))
        assert new_signals == [], "Signal engine must suppress signals when CB is tripped"


# ---------------------------------------------------------------------------
# Test 6 — Circuit breaker reset + session boundary
# ---------------------------------------------------------------------------


class TestCircuitBreakerResetAndSessionBoundary:
    """After a reset the engine accepts signals again. Outside trading hours the
    engine suppresses signals and daily counters reset on new session."""

    async def test_cb_reset_and_outside_hours_suppression(
        self, db_session_factory, paper_broker
    ):
        cb = CircuitBreaker(db_session_factory, {"max_daily_loss": 50.0})
        await cb.manual_trip()
        assert cb.is_tripped

        # Reset allows signals again
        cb.reset()
        assert not cb.is_tripped

        # --- Session boundary: outside 09:30–16:00 ET ---
        # Use 04:00 AM ET (08:00 UTC on April 1 = EDT, UTC-4)
        outside_hours = datetime(2026, 4, 1, 8, 0, 0)
        session_mgr = SessionManager(now_fn=lambda: outside_hours)

        assert session_mgr.should_suppress_signal(), \
            "04:00 AM ET should be outside trading hours"

        engine = SignalEngine(
            strategies=[_StaticStrategy(_make_signal())],
            broker=paper_broker,
            session_factory=db_session_factory,
            circuit_breaker=cb,
            session_manager=session_mgr,
        )
        signals = await engine.on_bar(_bar(5100.0, 1000))
        assert signals == [], "Signal engine must suppress signals outside trading hours"

        # --- Daily reset ---
        assert session_mgr.needs_daily_reset(outside_hours)
        paper_broker.reset_daily()
        session_mgr.mark_daily_reset_done(outside_hours)
        assert not session_mgr.needs_daily_reset(outside_hours)

        # Verify daily P&L was reset on the broker
        account = await paper_broker.get_account_info()
        assert account.daily_pnl == 0.0

        # --- In-hours on same day: signals flow ---
        # Use 10:00 AM ET (14:00 UTC on April 1)
        in_hours = datetime(2026, 4, 1, 14, 0, 0)
        session_mgr_live = SessionManager(now_fn=lambda: in_hours)

        assert not session_mgr_live.should_suppress_signal(), \
            "10:00 AM ET should be within trading hours"

        engine_live = SignalEngine(
            strategies=[_StaticStrategy(_make_signal())],
            broker=paper_broker,
            session_factory=db_session_factory,
            circuit_breaker=cb,
            session_manager=session_mgr_live,
        )
        live_signals = await engine_live.on_bar(_bar(5100.0, 1000))
        assert len(live_signals) >= 1, \
            "Signal engine must pass signals during trading hours after CB reset"


# ---------------------------------------------------------------------------
# Test 7 — Risk guardrails in the live pipeline
# ---------------------------------------------------------------------------


class TestRiskGuardrailsLivePipeline:
    """A signal with R:R < 1.5 is blocked by the risk manager in the full
    pipeline. No TradeRecord is created and an EventRecord is logged."""

    async def test_bad_rr_blocked_event_logged_no_trade(
        self, db_session_factory, paper_broker
    ):
        # Construct a signal with R:R = 1.0 (SL=10pts, TP=10pts)
        bad_signal = Signal(
            id=_uid(),
            instrument="ES",
            strategy_name="bad_rr_test",
            direction=Direction.LONG,
            entry_price=5100.0,
            stop_loss_price=5090.0,   # 10-point SL
            take_profit_price=5110.0,  # 10-point TP → R:R = 1.0 (below 1.5)
            rr_ratio=1.0,
            confidence_score=0.6,
            confluence_factors={},
            indicator_state={},
        )

        strategy = _StaticStrategy(bad_signal)
        rm = RiskManager()  # default: min_rr_ratio=1.5

        engine = SignalEngine(
            strategies=[strategy],
            broker=paper_broker,
            session_factory=db_session_factory,
            risk_manager=rm,
        )

        approved = await engine.on_bar(_bar(5100.0, 1000))

        # Signal must be blocked
        assert approved == [], "Bad R:R signal must not be approved"

        # SignalRecord exists but decision=REJECTED
        async with db_session_factory() as sess:
            result = await sess.execute(
                select(SignalRecord).where(SignalRecord.strategy_name == "bad_rr_test")
            )
            sig_rec = result.scalar_one_or_none()
        assert sig_rec is not None, "SignalRecord should be persisted even on rejection"
        assert sig_rec.decision == "REJECTED"

        # EventRecord with risk-rejection message must exist
        async with db_session_factory() as sess:
            result = await sess.execute(
                select(EventRecord).where(
                    EventRecord.name.like("%Risk rejection%bad_rr_test%")
                )
            )
            event = result.scalar_one_or_none()
        assert event is not None, "EventRecord should be logged for risk rejection"
        assert "R:R" in event.name or "ratio" in event.name.lower() or "1.00" in event.name

        # No TradeRecord must have been created
        async with db_session_factory() as sess:
            result = await sess.execute(
                select(TradeRecord).where(TradeRecord.signal_id == bad_signal.id)
            )
            assert result.scalar_one_or_none() is None, \
                "No TradeRecord should be created for a risk-rejected signal"
