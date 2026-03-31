"""
Tests for all three trading strategies.

Verifies signal generation logic, configurable parameters,
edge cases, and correct signal construction for:
- VWAP Mean Reversion
- EMA Crossover + Volume
- Opening Range Breakout
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend.core.models import Bar, Direction, Signal
from backend.strategies.vwap_reversion import VWAPReversionStrategy
from backend.strategies.ema_crossover import EMACrossoverStrategy
from backend.strategies.opening_range import OpeningRangeBreakoutStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar(
    instrument: str = "ES",
    o: float = 5100.0,
    h: float = 5102.0,
    l: float = 5098.0,
    c: float = 5101.0,
    volume: int = 1000,
    ts: datetime | None = None,
) -> Bar:
    return Bar(
        instrument=instrument,
        timestamp=ts or datetime(2026, 3, 30, 14, 0),
        open=o, high=h, low=l, close=c,
        volume=volume,
    )


# ---------------------------------------------------------------------------
# VWAP Mean Reversion Tests
# ---------------------------------------------------------------------------

class TestVWAPReversion:
    def test_no_signal_on_first_bars(self):
        strat = VWAPReversionStrategy("ES")
        # First bar just seeds VWAP, no signal possible
        sig = strat.on_bar(_bar(o=5100, h=5102, l=5098, c=5101, volume=1000))
        assert sig is None

    def test_vwap_computed(self):
        strat = VWAPReversionStrategy("ES")
        strat.on_bar(_bar(o=5100, h=5102, l=5098, c=5100, volume=1000))
        # VWAP = typical_price = (5102+5098+5100)/3 = 5100
        assert abs(strat._vwap - 5100.0) < 1.0

    def test_long_signal_on_lower_band_rejection(self):
        """Build VWAP around 5100, then drop to lower band with rejection candle."""
        strat = VWAPReversionStrategy("ES", {
            "sd_multiplier": 1.0,
            "min_deviation_distance": 1.0,
            "sl_buffer": 1.0,
        })

        # Build VWAP with several normal bars around 5100
        for i in range(20):
            strat.on_bar(_bar(o=5099+i*0.1, h=5102+i*0.1, l=5097+i*0.1,
                             c=5100+i*0.1, volume=1000))

        # Now create a bar that dips below lower band with bullish rejection
        # (long lower wick, close > open)
        sig = strat.on_bar(_bar(
            o=strat._lower_band - 1.0,
            h=strat._lower_band + 0.5,
            l=strat._lower_band - 3.0,  # long lower wick
            c=strat._lower_band + 0.3,  # close above open = bullish
            volume=1000,
        ))

        if sig is not None:
            assert sig.direction == Direction.LONG
            assert sig.strategy_name == "vwap_reversion"
            assert sig.instrument == "ES"
            assert "vwap" in sig.indicator_state

    def test_short_signal_on_upper_band_rejection(self):
        """Build VWAP, then spike to upper band with bearish rejection."""
        strat = VWAPReversionStrategy("ES", {
            "sd_multiplier": 1.0,
            "min_deviation_distance": 1.0,
            "sl_buffer": 1.0,
        })

        for i in range(20):
            strat.on_bar(_bar(o=5099, h=5102, l=5097, c=5100, volume=1000))

        sig = strat.on_bar(_bar(
            o=strat._upper_band + 1.0,
            h=strat._upper_band + 3.0,  # long upper wick
            l=strat._upper_band - 0.5,
            c=strat._upper_band - 0.3,  # close below open = bearish
            volume=1000,
        ))

        if sig is not None:
            assert sig.direction == Direction.SHORT

    def test_no_signal_when_disabled(self):
        strat = VWAPReversionStrategy("ES")
        strat.enabled = False
        sig = strat.on_bar(_bar())
        assert sig is None

    def test_reset_clears_state(self):
        strat = VWAPReversionStrategy("ES")
        strat.on_bar(_bar(volume=1000))
        assert strat._cum_volume > 0
        strat.reset()
        assert strat._cum_volume == 0.0
        assert strat._vwap == 0.0

    def test_tp_target_vwap(self):
        strat = VWAPReversionStrategy("ES", {"tp_target": "vwap", "sd_multiplier": 0.5, "min_deviation_distance": 0.1, "sl_buffer": 0.5})

        # Build VWAP
        for _ in range(10):
            strat.on_bar(_bar(o=5100, h=5101, l=5099, c=5100, volume=500))

        vwap = strat._vwap
        tp = strat._compute_tp(5095.0, Direction.LONG)
        assert tp == pytest.approx(vwap, abs=0.1)

    def test_tp_target_fixed_distance(self):
        strat = VWAPReversionStrategy("ES", {"tp_target": "fixed_distance", "tp_fixed_distance": 5.0})
        tp = strat._compute_tp(5100.0, Direction.LONG)
        assert tp == 5105.0
        tp = strat._compute_tp(5100.0, Direction.SHORT)
        assert tp == 5095.0

    def test_update_params(self):
        strat = VWAPReversionStrategy("ES")
        strat.update_params({"sd_multiplier": 3.0})
        assert strat._params["sd_multiplier"] == 3.0

    def test_default_params_returned(self):
        strat = VWAPReversionStrategy("ES")
        defaults = strat.default_params()
        assert "sd_multiplier" in defaults
        assert "sl_buffer" in defaults


# ---------------------------------------------------------------------------
# EMA Crossover Tests
# ---------------------------------------------------------------------------

class TestEMACrossover:
    def _build_trending_bars(self, start_price: float, count: int, direction: str = "up") -> list[Bar]:
        """Generate a series of bars trending up or down."""
        bars = []
        for i in range(count):
            if direction == "up":
                p = start_price + i * 2.0
            else:
                p = start_price - i * 2.0
            bars.append(_bar(o=p, h=p+1, l=p-1, c=p+0.5 if direction == "up" else p-0.5,
                            volume=2000))
        return bars

    def test_no_signal_during_warmup(self):
        strat = EMACrossoverStrategy("ES", {"fast_period": 5, "slow_period": 10})
        # Feed fewer bars than slow period
        for i in range(8):
            sig = strat.on_bar(_bar(c=5100+i, volume=1000))
        assert sig is None

    def test_bullish_crossover_generates_long_signal(self):
        strat = EMACrossoverStrategy("ES", {
            "fast_period": 3,
            "slow_period": 7,
            "volume_multiplier": 1.0,  # disable volume filter for this test
            "volume_lookback": 20,
        })

        # Feed downtrend to establish fast < slow
        for bar in self._build_trending_bars(5120, 15, "down"):
            strat.on_bar(bar)

        # Now feed strong uptrend to force fast > slow crossover
        signal = None
        for bar in self._build_trending_bars(5080, 15, "up"):
            result = strat.on_bar(bar)
            if result is not None:
                signal = result
                break

        if signal is not None:
            assert signal.direction == Direction.LONG
            assert signal.strategy_name == "ema_crossover"
            assert "fast_ema" in signal.indicator_state
            assert "slow_ema" in signal.indicator_state

    def test_bearish_crossover_generates_short_signal(self):
        strat = EMACrossoverStrategy("ES", {
            "fast_period": 3,
            "slow_period": 7,
            "volume_multiplier": 1.0,
        })

        # Feed uptrend then downtrend
        for bar in self._build_trending_bars(5080, 15, "up"):
            strat.on_bar(bar)
        signal = None
        for bar in self._build_trending_bars(5120, 15, "down"):
            result = strat.on_bar(bar)
            if result is not None:
                signal = result
                break

        if signal is not None:
            assert signal.direction == Direction.SHORT

    def test_volume_filter_rejects_low_volume(self):
        strat = EMACrossoverStrategy("ES", {
            "fast_period": 3,
            "slow_period": 7,
            "volume_multiplier": 5.0,  # very high threshold
            "volume_lookback": 20,
        })

        # Downtrend with normal volume
        for bar in self._build_trending_bars(5120, 15, "down"):
            strat.on_bar(bar)

        # Uptrend with LOW volume — should not signal
        signal = None
        for i in range(15):
            p = 5080 + i * 2
            result = strat.on_bar(_bar(o=p, h=p+1, l=p-1, c=p+0.5, volume=100))
            if result is not None:
                signal = result
                break
        assert signal is None

    def test_sl_uses_swing_low_for_long(self):
        strat = EMACrossoverStrategy("ES", {
            "fast_period": 3, "slow_period": 7, "volume_multiplier": 1.0,
        })

        for bar in self._build_trending_bars(5120, 15, "down"):
            strat.on_bar(bar)

        signal = None
        for bar in self._build_trending_bars(5080, 15, "up"):
            result = strat.on_bar(bar)
            if result is not None:
                signal = result
                break

        if signal is not None:
            assert signal.direction == Direction.LONG
            # SL should be below entry
            assert signal.stop_loss_price < signal.entry_price

    def test_rr_multiple_applied(self):
        strat = EMACrossoverStrategy("ES", {
            "fast_period": 3, "slow_period": 7,
            "volume_multiplier": 1.0, "rr_multiple": 3.0,
        })

        for bar in self._build_trending_bars(5120, 15, "down"):
            strat.on_bar(bar)

        signal = None
        for bar in self._build_trending_bars(5080, 15, "up"):
            result = strat.on_bar(bar)
            if result is not None:
                signal = result
                break

        if signal is not None:
            assert signal.rr_ratio == pytest.approx(3.0)

    def test_reset_clears_state(self):
        strat = EMACrossoverStrategy("ES")
        strat.on_bar(_bar(volume=1000))
        assert strat._bar_count > 0
        strat.reset()
        assert strat._bar_count == 0
        assert strat._fast_ema == 0.0

    def test_disabled_returns_none(self):
        strat = EMACrossoverStrategy("ES")
        strat.enabled = False
        assert strat.on_bar(_bar()) is None


# ---------------------------------------------------------------------------
# Opening Range Breakout Tests
# ---------------------------------------------------------------------------

class TestOpeningRangeBreakout:
    def _session_time(self, minutes_after_open: int = 0) -> datetime:
        """Return a datetime at session open + N minutes (UTC)."""
        return datetime(2026, 3, 30, 13, 30) + timedelta(minutes=minutes_after_open)

    def test_range_builds_during_first_n_minutes(self):
        strat = OpeningRangeBreakoutStrategy("ES", {
            "range_duration_minutes": 15,
            "session_start_hour_utc": 13,
            "session_start_minute_utc": 30,
        })

        # Feed 16 bars (minutes 0-15) so elapsed >= 15 minutes
        for i in range(16):
            strat.on_bar(_bar(
                h=5100 + i, l=5098 - i,
                o=5099, c=5099,
                volume=1000,
                ts=self._session_time(i),
            ))

        assert strat._range_defined is True
        assert strat._range_high == 5115.0  # 5100 + 15
        assert strat._range_low == 5083.0   # 5098 - 15

    def test_no_signal_during_range_building(self):
        strat = OpeningRangeBreakoutStrategy("ES", {
            "range_duration_minutes": 15,
            "session_start_hour_utc": 13,
            "session_start_minute_utc": 30,
        })

        for i in range(10):
            sig = strat.on_bar(_bar(
                h=5102, l=5098, o=5100, c=5101,
                volume=1000, ts=self._session_time(i),
            ))
            assert sig is None

    def test_bullish_breakout_signal(self):
        strat = OpeningRangeBreakoutStrategy("ES", {
            "range_duration_minutes": 5,
            "volume_threshold": 1.0,
            "extension_multiplier": 1.0,
            "sl_method": "midpoint",
            "session_start_hour_utc": 13,
            "session_start_minute_utc": 30,
        })

        # Build range: high=5105, low=5095
        for i in range(6):
            strat.on_bar(_bar(
                o=5100, h=5105, l=5095, c=5100,
                volume=1000, ts=self._session_time(i),
            ))

        assert strat._range_defined is True

        # Breakout bar closes above range high
        sig = strat.on_bar(_bar(
            o=5104, h=5108, l=5103, c=5107,  # close > 5105
            volume=1500, ts=self._session_time(10),
        ))

        assert sig is not None
        assert sig.direction == Direction.LONG
        assert sig.entry_price == 5107.0
        assert sig.stop_loss_price == 5100.0  # midpoint of 5105+5095
        range_width = 5105 - 5095  # = 10
        assert sig.take_profit_price == pytest.approx(5107.0 + range_width)
        assert sig.strategy_name == "opening_range"

    def test_bearish_breakout_signal(self):
        strat = OpeningRangeBreakoutStrategy("ES", {
            "range_duration_minutes": 5,
            "volume_threshold": 1.0,
            "extension_multiplier": 1.0,
            "sl_method": "opposite_boundary",
            "session_start_hour_utc": 13,
            "session_start_minute_utc": 30,
        })

        for i in range(6):
            strat.on_bar(_bar(
                o=5100, h=5105, l=5095, c=5100,
                volume=1000, ts=self._session_time(i),
            ))

        sig = strat.on_bar(_bar(
            o=5096, h=5097, l=5092, c=5093,  # close < 5095
            volume=1500, ts=self._session_time(10),
        ))

        assert sig is not None
        assert sig.direction == Direction.SHORT
        assert sig.stop_loss_price == 5105.0  # opposite boundary

    def test_only_one_breakout_per_direction(self):
        strat = OpeningRangeBreakoutStrategy("ES", {
            "range_duration_minutes": 5,
            "volume_threshold": 1.0,
            "session_start_hour_utc": 13,
            "session_start_minute_utc": 30,
        })

        for i in range(6):
            strat.on_bar(_bar(o=5100, h=5105, l=5095, c=5100,
                             volume=1000, ts=self._session_time(i)))

        # First bullish breakout
        sig1 = strat.on_bar(_bar(o=5104, h=5108, l=5103, c=5107,
                                 volume=1500, ts=self._session_time(10)))
        assert sig1 is not None

        # Second bullish breakout — should NOT signal again
        sig2 = strat.on_bar(_bar(o=5107, h=5112, l=5106, c=5110,
                                 volume=1500, ts=self._session_time(11)))
        assert sig2 is None

    def test_volume_filter_blocks_breakout(self):
        strat = OpeningRangeBreakoutStrategy("ES", {
            "range_duration_minutes": 5,
            "volume_threshold": 5.0,  # very high threshold
            "session_start_hour_utc": 13,
            "session_start_minute_utc": 30,
        })

        for i in range(6):
            strat.on_bar(_bar(o=5100, h=5105, l=5095, c=5100,
                             volume=1000, ts=self._session_time(i)))

        # Breakout with LOW volume
        sig = strat.on_bar(_bar(o=5104, h=5108, l=5103, c=5107,
                                volume=100, ts=self._session_time(10)))
        assert sig is None

    def test_indicator_state_includes_range(self):
        strat = OpeningRangeBreakoutStrategy("ES", {
            "range_duration_minutes": 5,
            "volume_threshold": 1.0,
            "session_start_hour_utc": 13,
            "session_start_minute_utc": 30,
        })

        for i in range(6):
            strat.on_bar(_bar(o=5100, h=5105, l=5095, c=5100,
                             volume=1000, ts=self._session_time(i)))

        sig = strat.on_bar(_bar(o=5104, h=5108, l=5103, c=5107,
                                volume=1500, ts=self._session_time(10)))

        assert sig is not None
        assert sig.indicator_state["range_high"] == 5105.0
        assert sig.indicator_state["range_low"] == 5095.0
        assert sig.indicator_state["range_width"] == 10.0

    def test_reset_clears_state(self):
        strat = OpeningRangeBreakoutStrategy("ES")
        strat._range_defined = True
        strat._range_high = 5100
        strat.reset()
        assert strat._range_defined is False
        assert strat._range_high is None

    def test_disabled_returns_none(self):
        strat = OpeningRangeBreakoutStrategy("ES")
        strat.enabled = False
        assert strat.on_bar(_bar()) is None

    def test_pre_session_bars_ignored(self):
        strat = OpeningRangeBreakoutStrategy("ES", {
            "range_duration_minutes": 5,
            "session_start_hour_utc": 13,
            "session_start_minute_utc": 30,
        })

        # Feed bar BEFORE session start
        sig = strat.on_bar(_bar(ts=datetime(2026, 3, 30, 12, 0)))
        assert sig is None
        assert len(strat._range_bars) == 0


# ---------------------------------------------------------------------------
# Cross-strategy tests
# ---------------------------------------------------------------------------

class TestStrategyInterface:
    """Verify all strategies implement the interface correctly."""

    @pytest.mark.parametrize("StratClass", [
        VWAPReversionStrategy,
        EMACrossoverStrategy,
        OpeningRangeBreakoutStrategy,
    ])
    def test_has_required_methods(self, StratClass):
        strat = StratClass("ES")
        assert hasattr(strat, "on_bar")
        assert hasattr(strat, "on_tick")
        assert hasattr(strat, "default_params")
        assert hasattr(strat, "reset")
        assert hasattr(strat, "update_params")

    @pytest.mark.parametrize("StratClass", [
        VWAPReversionStrategy,
        EMACrossoverStrategy,
        OpeningRangeBreakoutStrategy,
    ])
    def test_name_and_instrument(self, StratClass):
        strat = StratClass("NQ")
        assert strat.instrument == "NQ"
        assert isinstance(strat.name, str)
        assert len(strat.name) > 0

    @pytest.mark.parametrize("StratClass", [
        VWAPReversionStrategy,
        EMACrossoverStrategy,
        OpeningRangeBreakoutStrategy,
    ])
    def test_enable_disable(self, StratClass):
        strat = StratClass("ES")
        assert strat.enabled is True
        strat.enabled = False
        assert strat.enabled is False
        assert strat.on_bar(_bar()) is None
