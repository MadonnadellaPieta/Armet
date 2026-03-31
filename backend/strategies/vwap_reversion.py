"""
VWAP Mean Reversion Strategy.

Computes session VWAP with configurable standard deviation bands.
Signals when price touches/exceeds an outer band and shows reversion
(rejection candle pattern or delta shift).

Entry: reversion confirmation price
SL: beyond the deviation extreme + buffer
TP: VWAP or configurable distance
"""

from __future__ import annotations

import math
import uuid
from typing import Any

from backend.core.models import Bar, Direction, Signal
from backend.strategies.base_strategy import BaseStrategy


class VWAPReversionStrategy(BaseStrategy):
    """VWAP mean-reversion strategy with SD bands."""

    def __init__(self, instrument: str, params: dict[str, Any] | None = None) -> None:
        defaults = self.default_params()
        merged = {**defaults, **(params or {})}
        super().__init__("vwap_reversion", instrument, merged)

        # Session accumulators for VWAP calculation
        self._cum_volume: float = 0.0
        self._cum_tp_volume: float = 0.0    # sum(typical_price * volume)
        self._cum_tp2_volume: float = 0.0   # sum(typical_price^2 * volume)
        self._vwap: float = 0.0
        self._upper_band: float = 0.0
        self._lower_band: float = 0.0

        # Bar history for rejection candle detection
        self._prev_bar: Bar | None = None

    def default_params(self) -> dict[str, Any]:
        return {
            "sd_multiplier": 2.0,
            "min_deviation_distance": 2.0,
            "reversion_method": "rejection_candle",
            "tp_target": "vwap",
            "tp_fixed_distance": 4.0,
            "sl_buffer": 1.0,
        }

    def reset(self) -> None:
        self._cum_volume = 0.0
        self._cum_tp_volume = 0.0
        self._cum_tp2_volume = 0.0
        self._vwap = 0.0
        self._upper_band = 0.0
        self._lower_band = 0.0
        self._prev_bar = None

    def on_bar(self, bar: Bar) -> Signal | None:
        if not self._enabled:
            return None

        # Update VWAP
        self._update_vwap(bar)

        if self._vwap == 0.0 or self._cum_volume == 0.0:
            self._prev_bar = bar
            return None

        sd_mult = self._params["sd_multiplier"]
        min_dev = self._params["min_deviation_distance"]

        signal = None

        # Check for upper band touch + reversion (short signal)
        if bar.high >= self._upper_band:
            dev_distance = bar.high - self._vwap
            if dev_distance >= min_dev and self._check_reversion(bar, Direction.SHORT):
                signal = self._build_signal(bar, Direction.SHORT)

        # Check for lower band touch + reversion (long signal)
        elif bar.low <= self._lower_band:
            dev_distance = self._vwap - bar.low
            if dev_distance >= min_dev and self._check_reversion(bar, Direction.LONG):
                signal = self._build_signal(bar, Direction.LONG)

        self._prev_bar = bar
        return signal

    def _update_vwap(self, bar: Bar) -> None:
        """Incrementally update session VWAP and SD bands."""
        typical_price = (bar.high + bar.low + bar.close) / 3.0
        volume = max(bar.volume, 1)

        self._cum_volume += volume
        self._cum_tp_volume += typical_price * volume
        self._cum_tp2_volume += (typical_price ** 2) * volume

        self._vwap = self._cum_tp_volume / self._cum_volume

        # Standard deviation of typical price weighted by volume
        variance = (self._cum_tp2_volume / self._cum_volume) - (self._vwap ** 2)
        sd = math.sqrt(max(variance, 0.0))

        sd_mult = self._params["sd_multiplier"]
        self._upper_band = self._vwap + sd * sd_mult
        self._lower_band = self._vwap - sd * sd_mult

    def _check_reversion(self, bar: Bar, direction: Direction) -> bool:
        """Check for reversion confirmation."""
        method = self._params["reversion_method"]

        if method == "rejection_candle":
            return self._is_rejection_candle(bar, direction)
        elif method == "delta_shift":
            # Delta shift requires order flow data — handled by confluence filters.
            # At strategy level, accept bar close confirming direction.
            if direction == Direction.LONG:
                return bar.close > bar.open
            else:
                return bar.close < bar.open

        return False

    def _is_rejection_candle(self, bar: Bar, direction: Direction) -> bool:
        """Detect a rejection candle (long wick in the signal direction)."""
        body = abs(bar.close - bar.open)
        full_range = bar.high - bar.low
        if full_range == 0:
            return False

        if direction == Direction.LONG:
            # Bullish rejection: long lower wick, close near high
            lower_wick = min(bar.open, bar.close) - bar.low
            return lower_wick > body and bar.close > bar.open
        else:
            # Bearish rejection: long upper wick, close near low
            upper_wick = bar.high - max(bar.open, bar.close)
            return upper_wick > body and bar.close < bar.open

    def _build_signal(self, bar: Bar, direction: Direction) -> Signal:
        """Construct a Signal from the current bar state."""
        sl_buffer = self._params["sl_buffer"]

        entry_price = bar.close

        if direction == Direction.LONG:
            sl_price = bar.low - sl_buffer
            tp_price = self._compute_tp(entry_price, direction)
        else:
            sl_price = bar.high + sl_buffer
            tp_price = self._compute_tp(entry_price, direction)

        sl_distance = abs(entry_price - sl_price)
        tp_distance = abs(tp_price - entry_price)
        rr_ratio = tp_distance / sl_distance if sl_distance > 0 else 0.0

        return Signal(
            id=uuid.uuid4().hex,
            instrument=self._instrument,
            strategy_name=self._name,
            direction=direction,
            entry_price=entry_price,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            rr_ratio=round(rr_ratio, 2),
            confidence_score=0.0,  # Set by confidence scorer later
            indicator_state={
                "vwap": round(self._vwap, 2),
                "upper_band": round(self._upper_band, 2),
                "lower_band": round(self._lower_band, 2),
                "deviation": round(abs(entry_price - self._vwap), 2),
            },
        )

    def _compute_tp(self, entry_price: float, direction: Direction) -> float:
        """Compute take-profit price based on config."""
        tp_target = self._params["tp_target"]

        if tp_target == "vwap":
            return self._vwap
        else:
            distance = self._params["tp_fixed_distance"]
            if direction == Direction.LONG:
                return entry_price + distance
            else:
                return entry_price - distance
