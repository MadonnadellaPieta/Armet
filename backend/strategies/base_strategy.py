"""
Abstract base class for trading strategies.

Each strategy is a plugin that:
- Receives market data (ticks, bars) as input
- Produces Signal objects as output
- Has configurable parameters that can be changed at runtime
- Is completely decoupled from execution logic

This separation ensures the same strategy code can run against both
live market data and historical data for backtesting.
"""

from __future__ import annotations

import abc
from typing import Any

from backend.core.models import Bar, Signal, Tick


class BaseStrategy(abc.ABC):
    """Abstract base for all trading strategies.

    Subclasses must implement :meth:`on_bar` and/or :meth:`on_tick`.
    The signal engine calls these methods as market data arrives.
    """

    def __init__(self, name: str, instrument: str, params: dict[str, Any]) -> None:
        self._name = name
        self._instrument = instrument
        self._params = dict(params)
        self._enabled = True

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def instrument(self) -> str:
        return self._instrument

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def params(self) -> dict[str, Any]:
        return dict(self._params)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def update_params(self, new_params: dict[str, Any]) -> None:
        """Update strategy parameters at runtime.

        Only updates keys that already exist in the parameter dict
        to prevent typos from silently creating new params.
        """
        for key, value in new_params.items():
            if key in self._params:
                self._params[key] = value

    @abc.abstractmethod
    def default_params(self) -> dict[str, Any]:
        """Return the default parameter dictionary for this strategy.

        Used to initialize params and to document what is configurable.
        """

    # ------------------------------------------------------------------
    # Market data handlers
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def on_bar(self, bar: Bar) -> Signal | None:
        """Process a completed bar and optionally emit a signal.

        Args:
            bar: A completed OHLCV bar.

        Returns:
            A Signal if conditions are met, otherwise None.
        """

    def on_tick(self, tick: Tick) -> Signal | None:
        """Process a raw tick — override if the strategy needs tick-level data.

        Default implementation does nothing (most strategies work on bars).
        """
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset internal state (e.g. at session start).

        Override in subclasses that accumulate state across bars.
        """
