"""
Tests for the RiskManager service.

Covers every Apex Trader Funding guardrail (EVAL + PA phases), R:R gating,
hedging prohibition, naked-order rejection, contract limits, daily-loss
circuit breaker, and quantity adjustment.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.core.models import (
    AccountInfo,
    Direction,
    Phase,
    Position,
    Signal,
)
from backend.services.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _signal(
    direction: Direction = Direction.LONG,
    instrument: str = "ES",
    entry: float = 5000.0,
    sl: float = 4990.0,
    tp: float = 5020.0,
    rr: float = 2.0,
) -> Signal:
    return Signal(
        id="sig-001",
        instrument=instrument,
        strategy_name="test",
        direction=direction,
        entry_price=entry,
        stop_loss_price=sl,
        take_profit_price=tp,
        rr_ratio=rr,
        confidence_score=0.7,
        timestamp=datetime.utcnow(),
    )


def _position(
    instrument: str = "ES",
    direction: Direction = Direction.LONG,
    quantity: int = 1,
) -> Position:
    return Position(
        instrument=instrument,
        direction=direction,
        quantity=quantity,
        entry_price=5000.0,
        entry_timestamp=datetime.utcnow(),
    )


# ===========================================================================
# Tests
# ===========================================================================

class TestRiskManagerBasics:
    def test_clean_signal_approved(self):
        rm = RiskManager()
        result = rm.validate(_signal(), _account(), [])
        assert result.approved is True
        assert result.adjusted_quantity == 1

    def test_phase_in_result_details(self):
        rm = RiskManager()
        result = rm.validate(_signal(), _account(), [])
        assert result.details["phase"] == "EVAL"


class TestNakedOrder:
    def test_missing_sl_rejected(self):
        rm = RiskManager()
        sig = _signal(sl=0.0)
        result = rm.validate(sig, _account(), [])
        assert result.approved is False
        assert "Naked order" in result.reason

    def test_missing_tp_rejected(self):
        rm = RiskManager()
        sig = _signal(tp=0.0)
        result = rm.validate(sig, _account(), [])
        assert result.approved is False
        assert "Naked order" in result.reason


class TestRRRatio:
    def test_below_min_rejected(self):
        rm = RiskManager()
        sig = _signal(rr=1.0)
        result = rm.validate(sig, _account(), [])
        assert result.approved is False
        assert "below minimum" in result.reason

    def test_above_max_rejected(self):
        rm = RiskManager()
        sig = _signal(rr=6.0)
        result = rm.validate(sig, _account(), [])
        assert result.approved is False
        assert "above maximum" in result.reason

    def test_within_range_approved(self):
        rm = RiskManager()
        sig = _signal(rr=2.5)
        result = rm.validate(sig, _account(), [])
        assert result.approved is True

    def test_at_min_boundary_approved(self):
        rm = RiskManager()
        sig = _signal(rr=1.5)
        result = rm.validate(sig, _account(), [])
        assert result.approved is True

    def test_at_max_boundary_approved(self):
        rm = RiskManager()
        sig = _signal(rr=5.0)
        result = rm.validate(sig, _account(), [])
        assert result.approved is True


class TestHedging:
    def test_opposite_direction_rejected(self):
        rm = RiskManager()
        pos = _position(instrument="ES", direction=Direction.LONG)
        sig = _signal(direction=Direction.SHORT, instrument="ES")
        result = rm.validate(sig, _account(), [pos])
        assert result.approved is False
        assert "Hedging" in result.reason

    def test_same_direction_allowed(self):
        rm = RiskManager()
        pos = _position(instrument="ES", direction=Direction.LONG)
        sig = _signal(direction=Direction.LONG, instrument="ES")
        result = rm.validate(sig, _account(), [pos])
        assert result.approved is True

    def test_different_instrument_allowed(self):
        rm = RiskManager()
        pos = _position(instrument="NQ", direction=Direction.LONG)
        sig = _signal(direction=Direction.SHORT, instrument="ES")
        result = rm.validate(sig, _account(), [pos])
        assert result.approved is True


class TestMaxConcurrentPositions:
    def test_at_limit_rejected(self):
        rm = RiskManager({"max_concurrent_positions": 2})
        positions = [_position(), _position(instrument="NQ")]
        result = rm.validate(_signal(), _account(), positions)
        assert result.approved is False
        assert "concurrent positions" in result.reason

    def test_below_limit_approved(self):
        rm = RiskManager({"max_concurrent_positions": 2})
        result = rm.validate(_signal(), _account(), [_position()])
        assert result.approved is True


class TestMaxContracts:
    def test_eval_max_contracts_fully_used(self):
        rm = RiskManager({"eval_max_contracts": 8})
        positions = [_position(quantity=8)]
        result = rm.validate(_signal(), _account(), positions)
        assert result.approved is False
        assert "max contracts" in result.reason

    def test_eval_quantity_reduced_when_partial(self):
        rm = RiskManager({"eval_max_contracts": 8})
        positions = [_position(quantity=6)]
        result = rm.validate(_signal(), _account(), positions, requested_quantity=4)
        assert result.approved is True
        assert result.adjusted_quantity == 2
        assert "reduced" in result.reason

    def test_pa_max_contracts_lower_limit(self):
        rm = RiskManager({"phase": Phase.PA, "pa_max_contracts": 6})
        positions = [_position(quantity=6)]
        result = rm.validate(_signal(), _account(), positions)
        assert result.approved is False

    def test_within_contract_limit_no_reduction(self):
        rm = RiskManager({"eval_max_contracts": 8})
        positions = [_position(quantity=2)]
        result = rm.validate(_signal(), _account(), positions, requested_quantity=3)
        assert result.approved is True
        assert result.adjusted_quantity == 3


class TestDailyLoss:
    def test_daily_loss_limit_reached(self):
        rm = RiskManager({"max_daily_loss": 1500})
        acct = _account(daily_pnl=-1500.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is False
        assert "Daily loss" in result.reason

    def test_daily_loss_not_yet_reached(self):
        rm = RiskManager({"max_daily_loss": 1500})
        acct = _account(daily_pnl=-1000.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is True


class TestEvalDrawdown:
    def test_equity_below_eod_threshold(self):
        rm = RiskManager({"phase": Phase.EVAL})
        acct = _account(equity=106_999.0, eod_threshold=107_000.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is False
        assert "drawdown" in result.reason.lower()

    def test_equity_above_eod_threshold(self):
        rm = RiskManager({"phase": Phase.EVAL})
        acct = _account(equity=108_000.0, eod_threshold=107_000.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is True

    def test_drawdown_check_skipped_in_pa(self):
        rm = RiskManager({"phase": Phase.PA})
        acct = _account(equity=106_999.0, eod_threshold=107_000.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is True


class TestPASafetyNet:
    def test_below_safety_net_rejected(self):
        rm = RiskManager({"phase": Phase.PA, "pa_safety_net_balance": 103_100.0})
        acct = _account(balance=103_000.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is False
        assert "safety-net" in result.reason

    def test_above_safety_net_approved(self):
        rm = RiskManager({"phase": Phase.PA})
        acct = _account(balance=110_000.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is True

    def test_safety_net_skipped_in_eval(self):
        rm = RiskManager({"phase": Phase.EVAL})
        acct = _account(balance=100_000.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is True


class TestPANegativePnl:
    def test_negative_pnl_exceeds_ratio(self):
        rm = RiskManager({
            "phase": Phase.PA, "pa_negative_pnl_ratio": 0.30,
            "max_daily_loss": 999_999,  # disable daily-loss gate for this test
        })
        # 30% of 110k = 33k; daily_pnl = -35k exceeds
        acct = _account(balance=110_000.0, daily_pnl=-35_000.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is False
        assert "30%" in result.reason

    def test_negative_pnl_within_ratio(self):
        rm = RiskManager({
            "phase": Phase.PA, "pa_negative_pnl_ratio": 0.30,
            "max_daily_loss": 999_999,
        })
        acct = _account(balance=110_000.0, daily_pnl=-10_000.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is True

    def test_positive_pnl_always_passes(self):
        rm = RiskManager({"phase": Phase.PA})
        acct = _account(daily_pnl=5_000.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is True

    def test_negative_pnl_skipped_in_eval(self):
        rm = RiskManager({"phase": Phase.EVAL, "max_daily_loss": 999_999})
        acct = _account(daily_pnl=-50_000.0)
        result = rm.validate(_signal(), acct, [])
        assert result.approved is True


class TestPASLTPRatio:
    def test_sl_tp_ratio_exceeds_ceiling(self):
        rm = RiskManager({"phase": Phase.PA, "pa_max_sl_tp_ratio": 5.0})
        # SL dist = 60, TP dist = 10 → ratio = 6.0
        sig = _signal(entry=5000.0, sl=4940.0, tp=5010.0, rr=2.0)
        result = rm.validate(sig, _account(), [])
        assert result.approved is False
        assert "SL/TP ratio" in result.reason

    def test_sl_tp_ratio_within_ceiling(self):
        rm = RiskManager({"phase": Phase.PA, "pa_max_sl_tp_ratio": 5.0})
        # SL dist = 10, TP dist = 20 → ratio = 0.5
        sig = _signal(entry=5000.0, sl=4990.0, tp=5020.0, rr=2.0)
        result = rm.validate(sig, _account(), [])
        assert result.approved is True

    def test_sl_tp_ratio_skipped_in_eval(self):
        rm = RiskManager({"phase": Phase.EVAL})
        sig = _signal(entry=5000.0, sl=4940.0, tp=5010.0, rr=2.0)
        result = rm.validate(sig, _account(), [])
        assert result.approved is True

    def test_tp_zero_distance_rejected_in_pa(self):
        rm = RiskManager({"phase": Phase.PA})
        sig = _signal(entry=5000.0, sl=4990.0, tp=5000.0, rr=2.0)
        result = rm.validate(sig, _account(), [])
        assert result.approved is False
        assert "TP distance is zero" in result.reason


class TestUpdateParams:
    def test_update_existing_key(self):
        rm = RiskManager()
        rm.update_params({"max_daily_loss": 2000.0})
        assert rm._params["max_daily_loss"] == 2000.0

    def test_unknown_key_ignored(self):
        rm = RiskManager()
        rm.update_params({"nonexistent": 42})
        assert "nonexistent" not in rm._params
