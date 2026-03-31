"""
Order Flow Confluence Filter.

Analyzes delta (buying vs selling pressure) at the signal price level
using Level 2 / depth data. Aligned flow boosts confidence; divergent
flow reduces it.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from backend.core.models import Direction, OrderBook, Tick


class OrderFlowFilter:
    """Computes order flow delta and evaluates alignment with a signal direction."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        defaults = self.default_params()
        self._params = {**defaults, **(params or {})}

        # Track recent trades to compute delta
        self._buy_volume: int = 0
        self._sell_volume: int = 0
        self._recent_ticks: deque[Tick] = deque(maxlen=200)
        self._last_order_book: OrderBook | None = None

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "delta_threshold": 0.6,
            "weight": 0.25,
            "lookback_ticks": 100,
        }

    def update_params(self, new_params: dict[str, Any]) -> None:
        for k, v in new_params.items():
            if k in self._params:
                self._params[k] = v

    def on_tick(self, tick: Tick) -> None:
        """Process a tick to accumulate buy/sell volume.

        Classifies trades as buys (at or above ask) or sells (at or below bid).
        """
        self._recent_ticks.append(tick)

        if tick.price >= tick.ask:
            self._buy_volume += tick.size
        elif tick.price <= tick.bid:
            self._sell_volume += tick.size
        else:
            # Between bid and ask — split evenly
            half = tick.size // 2
            self._buy_volume += half
            self._sell_volume += tick.size - half

    def on_order_book(self, order_book: OrderBook) -> None:
        """Store latest order book for depth analysis."""
        self._last_order_book = order_book

    def evaluate(self, direction: Direction, price_level: float) -> dict[str, Any]:
        """Evaluate order flow alignment with the proposed signal direction.

        Returns a dict with:
            - aligned: bool — whether flow supports the direction
            - delta_ratio: float — ratio of directional volume (0.0-1.0)
            - confidence_adjustment: float — adjustment to apply (-1.0 to 1.0)
            - details: dict — breakdown data
        """
        total_volume = self._buy_volume + self._sell_volume
        if total_volume == 0:
            return {
                "aligned": False,
                "delta_ratio": 0.5,
                "confidence_adjustment": 0.0,
                "details": {"buy_volume": 0, "sell_volume": 0, "total": 0},
            }

        buy_ratio = self._buy_volume / total_volume
        sell_ratio = self._sell_volume / total_volume

        threshold = self._params["delta_threshold"]

        if direction == Direction.LONG:
            delta_ratio = buy_ratio
            aligned = buy_ratio >= threshold
        else:
            delta_ratio = sell_ratio
            aligned = sell_ratio >= threshold

        # Compute depth imbalance from order book if available
        depth_factor = self._evaluate_depth(direction, price_level)

        # Confidence adjustment: positive if aligned, negative if divergent
        raw_adjustment = (delta_ratio - 0.5) * 2.0  # scale to -1.0 to 1.0
        raw_adjustment = raw_adjustment * 0.5 + depth_factor * 0.5

        return {
            "aligned": aligned,
            "delta_ratio": round(delta_ratio, 3),
            "confidence_adjustment": round(raw_adjustment, 3),
            "details": {
                "buy_volume": self._buy_volume,
                "sell_volume": self._sell_volume,
                "total": total_volume,
                "buy_ratio": round(buy_ratio, 3),
                "sell_ratio": round(sell_ratio, 3),
                "depth_factor": round(depth_factor, 3),
            },
        }

    def _evaluate_depth(self, direction: Direction, price_level: float) -> float:
        """Evaluate order book depth imbalance around the price level.

        Returns a factor from -1.0 (strong opposition) to 1.0 (strong support).
        """
        if self._last_order_book is None:
            return 0.0

        bid_depth = sum(level.size for level in self._last_order_book.bids)
        ask_depth = sum(level.size for level in self._last_order_book.asks)
        total_depth = bid_depth + ask_depth

        if total_depth == 0:
            return 0.0

        if direction == Direction.LONG:
            # For longs, strong bids (support) is positive
            return (bid_depth - ask_depth) / total_depth
        else:
            # For shorts, strong asks (resistance above) is positive
            return (ask_depth - bid_depth) / total_depth

    def reset(self) -> None:
        """Reset accumulated volume data (e.g. at session start)."""
        self._buy_volume = 0
        self._sell_volume = 0
        self._recent_ticks.clear()
        self._last_order_book = None
