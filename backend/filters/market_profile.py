"""
Market Profile Confluence Filter.

Computes Value Area High (VAH), Value Area Low (VAL), and Point of Control
(POC) from current and prior session data. Proximity to these levels adjusts
signal confidence.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from backend.core.models import Bar, Direction


class MarketProfileFilter:
    """Compute market profile levels and evaluate signal proximity."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        defaults = self.default_params()
        self._params = {**defaults, **(params or {})}

        # TPO (Time-Price-Opportunity) distribution
        # Maps rounded price levels to volume at that level
        self._price_volume: dict[float, int] = defaultdict(int)
        self._tick_size: float = 0.25

        # Computed levels
        self._poc: float | None = None    # Point of Control
        self._vah: float | None = None    # Value Area High
        self._val: float | None = None    # Value Area Low

        # Prior session levels (set externally or computed at session end)
        self._prior_poc: float | None = None
        self._prior_vah: float | None = None
        self._prior_val: float | None = None

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "vah_val_lookback_days": 5,
            "weight": 0.25,
            "value_area_pct": 0.70,       # 70% of volume = value area
            "proximity_points": 3.0,       # how close price must be to a level
            "tick_size": 0.25,
        }

    def update_params(self, new_params: dict[str, Any]) -> None:
        for k, v in new_params.items():
            if k in self._params:
                self._params[k] = v
        self._tick_size = self._params.get("tick_size", 0.25)

    def on_bar(self, bar: Bar) -> None:
        """Accumulate bar data into the price-volume profile."""
        # Distribute volume across the bar's price range
        low_level = self._round_price(bar.low)
        high_level = self._round_price(bar.high)

        if low_level == high_level:
            self._price_volume[low_level] += bar.volume
        else:
            step = self._tick_size
            levels = []
            p = low_level
            while p <= high_level + step * 0.1:
                levels.append(p)
                p += step
            if levels:
                vol_per_level = bar.volume // len(levels)
                remainder = bar.volume % len(levels)
                for i, level in enumerate(levels):
                    self._price_volume[level] += vol_per_level + (1 if i < remainder else 0)

        # Recompute profile levels
        self._compute_levels()

    def set_prior_session(
        self, poc: float | None, vah: float | None, val: float | None
    ) -> None:
        """Set prior session market profile levels."""
        self._prior_poc = poc
        self._prior_vah = vah
        self._prior_val = val

    def evaluate(self, direction: Direction, price_level: float) -> dict[str, Any]:
        """Evaluate how the signal price relates to market profile levels.

        Returns:
            - aligned: bool
            - confidence_adjustment: float (-1.0 to 1.0)
            - details: dict with level values and proximity info
        """
        proximity = self._params["proximity_points"]
        factors: list[tuple[str, float, bool]] = []

        # Check proximity to each level
        for name, level in self._all_levels():
            if level is None:
                continue
            distance = abs(price_level - level)
            if distance <= proximity:
                is_support = price_level >= level
                is_resistance = price_level <= level

                if direction == Direction.LONG:
                    # Buying near support (at/above level) = aligned
                    aligned = is_support
                else:
                    # Selling near resistance (at/below level) = aligned
                    aligned = is_resistance

                # Closer = stronger adjustment
                strength = 1.0 - (distance / proximity) if proximity > 0 else 1.0
                factors.append((name, strength, aligned))

        if not factors:
            return {
                "aligned": False,
                "confidence_adjustment": 0.0,
                "details": self._level_details(),
            }

        # Aggregate: average of all nearby level signals
        total_adj = 0.0
        any_aligned = False
        for name, strength, aligned in factors:
            if aligned:
                total_adj += strength * 0.5
                any_aligned = True
            else:
                total_adj -= strength * 0.3

        adjustment = max(-1.0, min(1.0, total_adj / len(factors)))

        details = self._level_details()
        details["nearby_levels"] = [
            {"name": n, "strength": round(s, 3), "aligned": a}
            for n, s, a in factors
        ]

        return {
            "aligned": any_aligned,
            "confidence_adjustment": round(adjustment, 3),
            "details": details,
        }

    def _compute_levels(self) -> None:
        """Compute POC, VAH, VAL from accumulated price-volume data."""
        if not self._price_volume:
            return

        # POC = price level with highest volume
        self._poc = max(self._price_volume, key=self._price_volume.get)

        # Value Area = 70% of total volume centered on POC
        total_volume = sum(self._price_volume.values())
        va_target = total_volume * self._params["value_area_pct"]

        sorted_levels = sorted(self._price_volume.keys())
        if not sorted_levels:
            return

        poc_idx = sorted_levels.index(self._poc)
        va_volume = self._price_volume[self._poc]
        low_idx = poc_idx
        high_idx = poc_idx

        while va_volume < va_target and (low_idx > 0 or high_idx < len(sorted_levels) - 1):
            # Expand in the direction with more volume
            expand_low = self._price_volume.get(
                sorted_levels[low_idx - 1], 0
            ) if low_idx > 0 else 0
            expand_high = self._price_volume.get(
                sorted_levels[high_idx + 1], 0
            ) if high_idx < len(sorted_levels) - 1 else 0

            if expand_low >= expand_high and low_idx > 0:
                low_idx -= 1
                va_volume += self._price_volume[sorted_levels[low_idx]]
            elif high_idx < len(sorted_levels) - 1:
                high_idx += 1
                va_volume += self._price_volume[sorted_levels[high_idx]]
            else:
                break

        self._val = sorted_levels[low_idx]
        self._vah = sorted_levels[high_idx]

    def _all_levels(self) -> list[tuple[str, float | None]]:
        """Return all profile levels (current + prior session)."""
        return [
            ("poc", self._poc),
            ("vah", self._vah),
            ("val", self._val),
            ("prior_poc", self._prior_poc),
            ("prior_vah", self._prior_vah),
            ("prior_val", self._prior_val),
        ]

    def _level_details(self) -> dict[str, Any]:
        return {
            "poc": self._poc,
            "vah": self._vah,
            "val": self._val,
            "prior_poc": self._prior_poc,
            "prior_vah": self._prior_vah,
            "prior_val": self._prior_val,
        }

    def _round_price(self, price: float) -> float:
        """Round price to nearest tick size."""
        return round(round(price / self._tick_size) * self._tick_size, 4)

    def reset(self) -> None:
        """Reset current session data (keeps prior session levels)."""
        self._price_volume.clear()
        self._poc = None
        self._vah = None
        self._val = None
