"""
Opening Range Breakout Strategy.

Defines the high/low of the first N minutes of the regular session.
Signals on a breakout beyond the range with volume confirmation.

Entry: breakout price
SL: range midpoint or opposite boundary
TP: configurable extension (default 1x range width)
"""

from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Any

from backend.core.models import Bar, Direction, Signal
from backend.strategies.base_strategy import BaseStrategy


# Regular session start: 9:30 AM ET = 13:30 UTC
_SESSION_START_UTC = time(13, 30)


class OpeningRangeBreakoutStrategy(BaseStrategy):
    """Opening range breakout with volume confirmation."""

    def __init__(self, instrument: str, params: dict[str, Any] | None = None) -> None:
        defaults = self.default_params()
        merged = {**defaults, **(params or {})}
        super().__init__("opening_range", instrument, merged)

        self._range_high: float | None = None
        self._range_low: float | None = None
        self._range_defined = False
        self._range_bars: list[Bar] = []
        self._breakout_signaled_long = False
        self._breakout_signaled_short = False

        # Volume tracking for confirmation
        self._volumes: list[int] = []

    def default_params(self) -> dict[str, Any]:
        return {
            "range_duration_minutes": 15,
            "volume_threshold": 1.5,
            "extension_multiplier": 1.0,
            "sl_method": "midpoint",
            "session_start_hour_utc": 13,
            "session_start_minute_utc": 30,
        }

    def reset(self) -> None:
        self._range_high = None
        self._range_low = None
        self._range_defined = False
        self._range_bars = []
        self._breakout_signaled_long = False
        self._breakout_signaled_short = False
        self._volumes = []

    def on_bar(self, bar: Bar) -> Signal | None:
        if not self._enabled:
            return None

        self._volumes.append(bar.volume)
        session_start = self._get_session_start(bar.timestamp)

        # Phase 1: Building the opening range
        if not self._range_defined:
            if bar.timestamp >= session_start:
                self._range_bars.append(bar)

                # Update range high/low
                if self._range_high is None or bar.high > self._range_high:
                    self._range_high = bar.high
                if self._range_low is None or bar.low < self._range_low:
                    self._range_low = bar.low

                # Check if we have enough bars for the range duration
                range_minutes = self._params["range_duration_minutes"]
                if len(self._range_bars) > 0:
                    first_bar_time = self._range_bars[0].timestamp
                    elapsed = (bar.timestamp - first_bar_time).total_seconds() / 60.0
                    if elapsed >= range_minutes:
                        self._range_defined = True

            return None

        # Phase 2: Watch for breakouts
        if self._range_high is None or self._range_low is None:
            return None

        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return None

        # Bullish breakout
        if (
            not self._breakout_signaled_long
            and bar.close > self._range_high
            and self._volume_confirmed(bar)
        ):
            self._breakout_signaled_long = True
            return self._build_signal(bar, Direction.LONG, range_width)

        # Bearish breakout
        if (
            not self._breakout_signaled_short
            and bar.close < self._range_low
            and self._volume_confirmed(bar)
        ):
            self._breakout_signaled_short = True
            return self._build_signal(bar, Direction.SHORT, range_width)

        return None

    def _volume_confirmed(self, bar: Bar) -> bool:
        """Check volume exceeds threshold * average."""
        if len(self._volumes) < 2:
            return True
        prev_volumes = self._volumes[:-1]
        avg = sum(prev_volumes) / len(prev_volumes)
        if avg == 0:
            return True
        return bar.volume >= avg * self._params["volume_threshold"]

    def _build_signal(
        self, bar: Bar, direction: Direction, range_width: float
    ) -> Signal:
        entry_price = bar.close
        ext_mult = self._params["extension_multiplier"]
        sl_method = self._params["sl_method"]

        if direction == Direction.LONG:
            if sl_method == "midpoint":
                sl_price = (self._range_high + self._range_low) / 2.0
            else:  # opposite_boundary
                sl_price = self._range_low
            tp_price = entry_price + range_width * ext_mult
        else:
            if sl_method == "midpoint":
                sl_price = (self._range_high + self._range_low) / 2.0
            else:
                sl_price = self._range_high
            tp_price = entry_price - range_width * ext_mult

        sl_distance = abs(entry_price - sl_price)
        tp_distance = abs(tp_price - entry_price)
        rr_ratio = tp_distance / sl_distance if sl_distance > 0 else 0.0

        return Signal(
            id=uuid.uuid4().hex,
            instrument=self._instrument,
            strategy_name=self._name,
            direction=direction,
            entry_price=entry_price,
            stop_loss_price=round(sl_price, 2),
            take_profit_price=round(tp_price, 2),
            rr_ratio=round(rr_ratio, 2),
            confidence_score=0.0,
            indicator_state={
                "range_high": self._range_high,
                "range_low": self._range_low,
                "range_width": round(range_width, 2),
                "breakout_distance": round(
                    abs(bar.close - (
                        self._range_high if direction == Direction.LONG
                        else self._range_low
                    )), 2
                ),
            },
        )

    def _get_session_start(self, ts: datetime) -> datetime:
        """Return the session start time for the given bar's date."""
        h = self._params["session_start_hour_utc"]
        m = self._params["session_start_minute_utc"]
        return ts.replace(hour=h, minute=m, second=0, microsecond=0)
