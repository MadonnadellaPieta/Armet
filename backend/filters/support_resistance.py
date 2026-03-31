"""
Support / Resistance Confluence Filter.

Auto-detects key levels from:
- Prior session high / low
- Overnight high / low
- Weekly / monthly levels (if provided)

Signal alignment with S/R adjusts confidence.
"""

from __future__ import annotations

from typing import Any

from backend.core.models import Bar, Direction


class SupportResistanceFilter:
    """Track and evaluate S/R levels for signal confluence."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        defaults = self.default_params()
        self._params = {**defaults, **(params or {})}

        # Auto-detected levels from current session
        self._session_high: float | None = None
        self._session_low: float | None = None

        # Externally set levels
        self._prior_session_high: float | None = None
        self._prior_session_low: float | None = None
        self._overnight_high: float | None = None
        self._overnight_low: float | None = None
        self._weekly_high: float | None = None
        self._weekly_low: float | None = None
        self._monthly_high: float | None = None
        self._monthly_low: float | None = None

        # Custom user-defined levels
        self._custom_levels: list[tuple[str, float]] = []

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "level_proximity_points": 3.0,
            "weight": 0.25,
        }

    def update_params(self, new_params: dict[str, Any]) -> None:
        for k, v in new_params.items():
            if k in self._params:
                self._params[k] = v

    def on_bar(self, bar: Bar) -> None:
        """Track session high/low from incoming bars."""
        if self._session_high is None or bar.high > self._session_high:
            self._session_high = bar.high
        if self._session_low is None or bar.low < self._session_low:
            self._session_low = bar.low

    def set_levels(
        self,
        prior_session_high: float | None = None,
        prior_session_low: float | None = None,
        overnight_high: float | None = None,
        overnight_low: float | None = None,
        weekly_high: float | None = None,
        weekly_low: float | None = None,
        monthly_high: float | None = None,
        monthly_low: float | None = None,
    ) -> None:
        """Set reference S/R levels (typically at session start)."""
        if prior_session_high is not None:
            self._prior_session_high = prior_session_high
        if prior_session_low is not None:
            self._prior_session_low = prior_session_low
        if overnight_high is not None:
            self._overnight_high = overnight_high
        if overnight_low is not None:
            self._overnight_low = overnight_low
        if weekly_high is not None:
            self._weekly_high = weekly_high
        if weekly_low is not None:
            self._weekly_low = weekly_low
        if monthly_high is not None:
            self._monthly_high = monthly_high
        if monthly_low is not None:
            self._monthly_low = monthly_low

    def add_custom_level(self, name: str, price: float) -> None:
        """Add a user-defined S/R level."""
        self._custom_levels.append((name, price))

    def evaluate(self, direction: Direction, price_level: float) -> dict[str, Any]:
        """Evaluate signal proximity to S/R levels.

        For LONG signals:
          - Price near support (low levels) = aligned (bouncing off support)
          - Price near resistance (high levels) = divergent (running into ceiling)

        For SHORT signals:
          - Price near resistance (high levels) = aligned (rejecting from resistance)
          - Price near support (low levels) = divergent (selling into floor)

        Returns:
            - aligned: bool
            - confidence_adjustment: float
            - details: dict with nearby levels
        """
        proximity = self._params["level_proximity_points"]
        nearby: list[dict[str, Any]] = []

        for name, level, is_high_level in self._all_levels():
            if level is None:
                continue
            distance = abs(price_level - level)
            if distance <= proximity:
                strength = 1.0 - (distance / proximity) if proximity > 0 else 1.0

                if direction == Direction.LONG:
                    aligned = not is_high_level  # near support = good for longs
                else:
                    aligned = is_high_level       # near resistance = good for shorts

                nearby.append({
                    "name": name,
                    "level": level,
                    "distance": round(distance, 2),
                    "strength": round(strength, 3),
                    "aligned": aligned,
                })

        if not nearby:
            return {
                "aligned": False,
                "confidence_adjustment": 0.0,
                "details": {"nearby_levels": [], "all_levels": self._level_summary()},
            }

        total_adj = 0.0
        any_aligned = False
        for entry in nearby:
            if entry["aligned"]:
                total_adj += entry["strength"] * 0.5
                any_aligned = True
            else:
                total_adj -= entry["strength"] * 0.3

        adjustment = max(-1.0, min(1.0, total_adj / len(nearby)))

        return {
            "aligned": any_aligned,
            "confidence_adjustment": round(adjustment, 3),
            "details": {
                "nearby_levels": nearby,
                "all_levels": self._level_summary(),
            },
        }

    def _all_levels(self) -> list[tuple[str, float | None, bool]]:
        """Return (name, price, is_high_level) for all known S/R levels."""
        levels: list[tuple[str, float | None, bool]] = [
            ("prior_session_high", self._prior_session_high, True),
            ("prior_session_low", self._prior_session_low, False),
            ("overnight_high", self._overnight_high, True),
            ("overnight_low", self._overnight_low, False),
            ("weekly_high", self._weekly_high, True),
            ("weekly_low", self._weekly_low, False),
            ("monthly_high", self._monthly_high, True),
            ("monthly_low", self._monthly_low, False),
            ("session_high", self._session_high, True),
            ("session_low", self._session_low, False),
        ]
        for name, price in self._custom_levels:
            levels.append((name, price, price > (self._session_high or price) * 0.5))
        return levels

    def _level_summary(self) -> dict[str, float | None]:
        return {
            "prior_session_high": self._prior_session_high,
            "prior_session_low": self._prior_session_low,
            "overnight_high": self._overnight_high,
            "overnight_low": self._overnight_low,
            "weekly_high": self._weekly_high,
            "weekly_low": self._weekly_low,
            "monthly_high": self._monthly_high,
            "monthly_low": self._monthly_low,
            "session_high": self._session_high,
            "session_low": self._session_low,
        }

    def reset(self) -> None:
        """Reset session-specific data (keeps prior/weekly/monthly levels)."""
        self._session_high = None
        self._session_low = None
