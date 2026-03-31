"""
EMA Crossover + Volume Strategy.

Signals when fast EMA crosses slow EMA, confirmed by above-average volume.

Entry: on crossover bar close
SL: beyond recent swing high/low
TP: configurable R:R multiple of SL distance
"""

from __future__ import annotations

import uuid
from collections import deque
from typing import Any

from backend.core.models import Bar, Direction, Signal
from backend.strategies.base_strategy import BaseStrategy


class EMACrossoverStrategy(BaseStrategy):
    """EMA crossover with volume confirmation."""

    def __init__(self, instrument: str, params: dict[str, Any] | None = None) -> None:
        defaults = self.default_params()
        merged = {**defaults, **(params or {})}
        super().__init__("ema_crossover", instrument, merged)

        self._fast_ema: float = 0.0
        self._slow_ema: float = 0.0
        self._fast_initialized = False
        self._slow_initialized = False
        self._prev_fast_ema: float = 0.0
        self._prev_slow_ema: float = 0.0

        # Volume lookback
        max_lookback = merged["volume_lookback"]
        self._volumes: deque[int] = deque(maxlen=max_lookback)

        # Swing tracking for SL placement
        self._recent_highs: deque[float] = deque(maxlen=20)
        self._recent_lows: deque[float] = deque(maxlen=20)

        self._bar_count = 0

    def default_params(self) -> dict[str, Any]:
        return {
            "fast_period": 9,
            "slow_period": 21,
            "volume_multiplier": 1.5,
            "volume_lookback": 20,
            "timeframe_minutes": 5,
            "rr_multiple": 2.0,
        }

    def reset(self) -> None:
        self._fast_ema = 0.0
        self._slow_ema = 0.0
        self._fast_initialized = False
        self._slow_initialized = False
        self._prev_fast_ema = 0.0
        self._prev_slow_ema = 0.0
        self._volumes.clear()
        self._recent_highs.clear()
        self._recent_lows.clear()
        self._bar_count = 0

    def on_bar(self, bar: Bar) -> Signal | None:
        if not self._enabled:
            return None

        self._bar_count += 1

        # Track swings and volume
        self._recent_highs.append(bar.high)
        self._recent_lows.append(bar.low)
        self._volumes.append(bar.volume)

        # Save previous EMAs before updating
        self._prev_fast_ema = self._fast_ema
        self._prev_slow_ema = self._slow_ema

        # Update EMAs
        self._fast_ema = self._update_ema(
            bar.close, self._fast_ema, self._params["fast_period"],
            self._fast_initialized,
        )
        self._slow_ema = self._update_ema(
            bar.close, self._slow_ema, self._params["slow_period"],
            self._slow_initialized,
        )

        if self._bar_count >= self._params["fast_period"]:
            self._fast_initialized = True
        if self._bar_count >= self._params["slow_period"]:
            self._slow_initialized = True

        # Need both EMAs initialized and at least one previous bar
        if not (self._fast_initialized and self._slow_initialized):
            return None
        if self._prev_fast_ema == 0.0 or self._prev_slow_ema == 0.0:
            return None

        # Detect crossover
        bullish_cross = (
            self._prev_fast_ema <= self._prev_slow_ema
            and self._fast_ema > self._slow_ema
        )
        bearish_cross = (
            self._prev_fast_ema >= self._prev_slow_ema
            and self._fast_ema < self._slow_ema
        )

        if not (bullish_cross or bearish_cross):
            return None

        # Volume confirmation
        if not self._volume_confirmed(bar):
            return None

        direction = Direction.LONG if bullish_cross else Direction.SHORT
        return self._build_signal(bar, direction)

    @staticmethod
    def _update_ema(price: float, prev_ema: float, period: int, initialized: bool) -> float:
        """Calculate EMA with standard smoothing factor."""
        multiplier = 2.0 / (period + 1)
        if not initialized:
            return price  # seed with first price
        return price * multiplier + prev_ema * (1 - multiplier)

    def _volume_confirmed(self, bar: Bar) -> bool:
        """Check if current bar volume exceeds the lookback average by the multiplier."""
        if len(self._volumes) < 2:
            return True  # not enough history, allow signal

        # Average of previous volumes (excluding current bar which was just appended)
        prev_volumes = list(self._volumes)[:-1]
        if not prev_volumes:
            return True
        avg_volume = sum(prev_volumes) / len(prev_volumes)
        if avg_volume == 0:
            return True

        return bar.volume >= avg_volume * self._params["volume_multiplier"]

    def _build_signal(self, bar: Bar, direction: Direction) -> Signal:
        """Construct a Signal with swing-based SL and R:R-based TP."""
        entry_price = bar.close

        if direction == Direction.LONG:
            # SL below recent swing low
            sl_price = min(self._recent_lows) if self._recent_lows else bar.low
            sl_distance = entry_price - sl_price
            tp_distance = sl_distance * self._params["rr_multiple"]
            tp_price = entry_price + tp_distance
        else:
            # SL above recent swing high
            sl_price = max(self._recent_highs) if self._recent_highs else bar.high
            sl_distance = sl_price - entry_price
            tp_distance = sl_distance * self._params["rr_multiple"]
            tp_price = entry_price - tp_distance

        rr_ratio = self._params["rr_multiple"] if sl_distance > 0 else 0.0

        avg_vol = (
            sum(list(self._volumes)[:-1]) / max(len(self._volumes) - 1, 1)
            if len(self._volumes) > 1 else 0
        )

        return Signal(
            id=uuid.uuid4().hex,
            instrument=self._instrument,
            strategy_name=self._name,
            direction=direction,
            entry_price=entry_price,
            stop_loss_price=round(sl_price, 2),
            take_profit_price=round(tp_price, 2),
            rr_ratio=round(rr_ratio, 2),
            confidence_score=0.0,  # Set by confidence scorer later
            indicator_state={
                "fast_ema": round(self._fast_ema, 2),
                "slow_ema": round(self._slow_ema, 2),
                "volume": bar.volume,
                "avg_volume": round(avg_vol, 0),
                "volume_ratio": round(
                    bar.volume / avg_vol if avg_vol > 0 else 0, 2
                ),
            },
        )
