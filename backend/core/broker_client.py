"""
Abstract base class for broker connections.

Both RithmicClient and TradovateClient implement this interface.
The rest of the system interacts only with this ABC, making the
broker implementation swappable without touching strategy, risk,
or order management code.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from backend.core.models import (
    AccountInfo,
    BracketOrderRequest,
    BracketOrderResult,
    ConnectionStatus,
    OrderBook,
    OrderUpdate,
    Position,
    Tick,
)

# Callback type aliases
TickCallback = Callable[[Tick], Coroutine[Any, Any, None]]
OrderBookCallback = Callable[[OrderBook], Coroutine[Any, Any, None]]
OrderUpdateCallback = Callable[[OrderUpdate], Coroutine[Any, Any, None]]
ConnectionCallback = Callable[[ConnectionStatus], Coroutine[Any, Any, None]]


class BrokerClient(abc.ABC):
    """Abstract broker client interface.

    Implementations must be fully async and support the event-driven
    architecture of the trading system.  Market data arrives via
    registered callbacks; the system does NOT poll.
    """

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def connect(self) -> None:
        """Authenticate and establish connections to the broker.

        Raises:
            ConnectionError: If authentication or connection fails.
        """

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close all connections."""

    @abc.abstractmethod
    async def is_connected(self) -> bool:
        """Return True if the client is connected and authenticated."""

    # ------------------------------------------------------------------
    # Market data subscriptions
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def subscribe_market_data(
        self,
        instrument: str,
        on_tick: TickCallback,
    ) -> None:
        """Subscribe to real-time tick data for *instrument*.

        Each incoming tick invokes *on_tick* asynchronously.

        Args:
            instrument: Symbol to subscribe to (e.g. "ESZ4", "MESH4").
            on_tick: Async callback invoked on every tick.
        """

    @abc.abstractmethod
    async def unsubscribe_market_data(self, instrument: str) -> None:
        """Stop receiving tick data for *instrument*."""

    @abc.abstractmethod
    async def subscribe_order_book(
        self,
        instrument: str,
        on_update: OrderBookCallback,
    ) -> None:
        """Subscribe to Level 2 order book updates for *instrument*.

        Args:
            instrument: Symbol to subscribe to.
            on_update: Async callback invoked on every order book change.
        """

    @abc.abstractmethod
    async def unsubscribe_order_book(self, instrument: str) -> None:
        """Stop receiving order book updates for *instrument*."""

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def place_bracket_order(
        self,
        request: BracketOrderRequest,
    ) -> BracketOrderResult:
        """Place a bracket order (entry + stop-loss + take-profit).

        The broker must send all three legs atomically where possible.

        Args:
            request: Fully populated bracket order request.

        Returns:
            BracketOrderResult with IDs for all three order legs.

        Raises:
            OrderError: If the order is rejected by the broker.
        """

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order.

        Returns True if the cancellation was accepted by the broker.
        """

    @abc.abstractmethod
    async def modify_order(
        self,
        order_id: str,
        new_price: float | None = None,
        new_quantity: int | None = None,
    ) -> bool:
        """Modify a pending order's price and/or quantity.

        Returns True if the modification was accepted.
        """

    @abc.abstractmethod
    async def flatten_all(self) -> int:
        """Close all open positions immediately (market orders).

        Returns the number of positions that were flattened.
        """

    # ------------------------------------------------------------------
    # Position & account queries
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return all currently open positions."""

    @abc.abstractmethod
    async def get_account_info(self) -> AccountInfo:
        """Return the current account balance and status."""

    # ------------------------------------------------------------------
    # Event registration
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def on_order_update(self, callback: OrderUpdateCallback) -> None:
        """Register a callback invoked on every order status change.

        Multiple callbacks may be registered; all are invoked.
        """

    @abc.abstractmethod
    def on_connection_status_change(self, callback: ConnectionCallback) -> None:
        """Register a callback invoked when connection status changes.

        Used by the session manager for auto-reconnect / flatten logic.
        """


class BrokerError(Exception):
    """Base exception for broker-related errors."""


class OrderError(BrokerError):
    """Raised when an order is rejected or fails."""


class ConnectionError(BrokerError):
    """Raised when a connection attempt fails."""
