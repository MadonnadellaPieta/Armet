"""
Rithmic implementation of BrokerClient using the async-rithmic library.

Wraps async_rithmic.RithmicClient and translates between the library's
event model and our BrokerClient ABC interface.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from backend.core.broker_client import (
    BrokerClient,
    ConnectionCallback,
    ConnectionError,
    OrderBookCallback,
    OrderError,
    OrderUpdateCallback,
    TickCallback,
)
from backend.core.models import (
    AccountInfo,
    BracketOrderRequest,
    BracketOrderResult,
    ConnectionStatus,
    Direction,
    OrderBook,
    OrderBookLevel,
    OrderStatus,
    OrderUpdate,
    Position,
    Tick,
)

logger = logging.getLogger(__name__)

try:
    from async_rithmic import RithmicClient as _RithmicClient
    from async_rithmic import OrderType as ROrderType
    from async_rithmic import TransactionType as RTransactionType

    HAS_ASYNC_RITHMIC = True
except ImportError:
    HAS_ASYNC_RITHMIC = False
    logger.warning(
        "async-rithmic not installed. RithmicClient will not be available. "
        "Install with: pip install async-rithmic"
    )


def _uid() -> str:
    return uuid.uuid4().hex


class RithmicBrokerClient(BrokerClient):
    """Rithmic broker client implementation.

    Requires the async-rithmic package and valid Rithmic credentials.
    """

    def __init__(
        self,
        user: str,
        password: str,
        system_name: str = "Rithmic Paper Trading",
        app_name: str = "TradingSignalSystem",
        app_version: str = "1.0",
        url: str = "",
    ) -> None:
        if not HAS_ASYNC_RITHMIC:
            raise ImportError(
                "async-rithmic is required for RithmicBrokerClient. "
                "Install with: pip install async-rithmic"
            )

        self._user = user
        self._password = password
        self._system_name = system_name
        self._app_name = app_name
        self._app_version = app_version
        self._url = url

        self._client: _RithmicClient | None = None
        self._connected = False
        self._connection_status = ConnectionStatus.DISCONNECTED

        # Callback registries
        self._tick_callbacks: dict[str, TickCallback] = {}
        self._order_book_callbacks: dict[str, OrderBookCallback] = {}
        self._order_update_callbacks: list[OrderUpdateCallback] = []
        self._connection_callbacks: list[ConnectionCallback] = []

        # Position tracking
        self._positions: dict[str, Position] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        try:
            await self._set_connection_status(ConnectionStatus.CONNECTING)

            self._client = _RithmicClient(
                user=self._user,
                password=self._password,
                system_name=self._system_name,
                app_name=self._app_name,
                app_version=self._app_version,
                url=self._url,
            )

            # Register internal event handlers
            self._client.on_connected += self._handle_connected
            self._client.on_disconnected += self._handle_disconnected

            await self._client.connect()
            self._connected = True
            await self._set_connection_status(ConnectionStatus.CONNECTED)
            logger.info("Connected to Rithmic (%s)", self._system_name)

        except Exception as exc:
            await self._set_connection_status(ConnectionStatus.ERROR)
            raise ConnectionError(f"Failed to connect to Rithmic: {exc}") from exc

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                logger.exception("Error during Rithmic disconnect")
            finally:
                self._connected = False
                self._client = None
                await self._set_connection_status(ConnectionStatus.DISCONNECTED)

    async def is_connected(self) -> bool:
        return self._connected and self._client is not None

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def subscribe_market_data(
        self, instrument: str, on_tick: TickCallback
    ) -> None:
        self._ensure_connected()
        self._tick_callbacks[instrument] = on_tick

        # Register the tick handler if this is the first subscription
        if len(self._tick_callbacks) == 1:
            self._client.on_tick += self._handle_tick

        exchange = self._resolve_exchange(instrument)
        await self._client.subscribe_to_market_data(
            security_code=instrument,
            exchange=exchange,
        )
        logger.info("Subscribed to market data: %s", instrument)

    async def unsubscribe_market_data(self, instrument: str) -> None:
        self._ensure_connected()
        self._tick_callbacks.pop(instrument, None)

        exchange = self._resolve_exchange(instrument)
        await self._client.unsubscribe_from_market_data(
            security_code=instrument,
            exchange=exchange,
        )

    async def subscribe_order_book(
        self, instrument: str, on_update: OrderBookCallback
    ) -> None:
        self._ensure_connected()
        self._order_book_callbacks[instrument] = on_update

        if len(self._order_book_callbacks) == 1:
            self._client.on_order_book += self._handle_order_book

        exchange = self._resolve_exchange(instrument)
        await self._client.subscribe_to_market_depth(
            security_code=instrument,
            exchange=exchange,
        )

    async def unsubscribe_order_book(self, instrument: str) -> None:
        self._ensure_connected()
        self._order_book_callbacks.pop(instrument, None)

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def place_bracket_order(
        self, request: BracketOrderRequest
    ) -> BracketOrderResult:
        self._ensure_connected()

        entry_id = f"entry_{_uid()}"
        sl_id = f"sl_{_uid()}"
        tp_id = f"tp_{_uid()}"

        transaction_type = (
            RTransactionType.BUY
            if request.direction == Direction.LONG
            else RTransactionType.SELL
        )

        exchange = self._resolve_exchange(request.instrument)

        try:
            await self._client.submit_order(
                order_id=entry_id,
                security_code=request.instrument,
                exchange=exchange,
                quantity=request.quantity,
                order_type=ROrderType.MARKET,
                transaction_type=transaction_type,
                stop_loss=request.stop_loss_price,
                take_profit=request.take_profit_price,
            )

            logger.info(
                "Bracket order placed: %s %s %s x%d SL=%.2f TP=%.2f",
                entry_id, request.direction.value, request.instrument,
                request.quantity, request.stop_loss_price, request.take_profit_price,
            )

            return BracketOrderResult(
                entry_order_id=entry_id,
                stop_loss_order_id=sl_id,
                take_profit_order_id=tp_id,
                status=OrderStatus.SUBMITTED,
            )

        except Exception as exc:
            raise OrderError(f"Failed to place bracket order: {exc}") from exc

    async def cancel_order(self, order_id: str) -> bool:
        self._ensure_connected()
        try:
            await self._client.cancel_order(order_id=order_id)
            return True
        except Exception:
            logger.exception("Failed to cancel order %s", order_id)
            return False

    async def modify_order(
        self,
        order_id: str,
        new_price: float | None = None,
        new_quantity: int | None = None,
    ) -> bool:
        self._ensure_connected()
        try:
            kwargs: dict[str, Any] = {"order_id": order_id}
            if new_price is not None:
                kwargs["price"] = new_price
            if new_quantity is not None:
                kwargs["quantity"] = new_quantity
            await self._client.update_order(**kwargs)
            return True
        except Exception:
            logger.exception("Failed to modify order %s", order_id)
            return False

    async def flatten_all(self) -> int:
        self._ensure_connected()
        positions = await self.get_positions()
        count = 0
        for pos in positions:
            try:
                close_id = f"flatten_{_uid()}"
                transaction_type = (
                    RTransactionType.SELL
                    if pos.direction == Direction.LONG
                    else RTransactionType.BUY
                )
                exchange = self._resolve_exchange(pos.instrument)
                await self._client.submit_order(
                    order_id=close_id,
                    security_code=pos.instrument,
                    exchange=exchange,
                    quantity=pos.quantity,
                    order_type=ROrderType.MARKET,
                    transaction_type=transaction_type,
                )
                count += 1
            except Exception:
                logger.exception("Failed to flatten position: %s", pos.instrument)
        return count

    # ------------------------------------------------------------------
    # Position & account queries
    # ------------------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def get_account_info(self) -> AccountInfo:
        self._ensure_connected()
        # Rithmic account info comes via the account plant
        # For now, return a basic snapshot; will be enhanced when
        # account plant events are wired up
        return AccountInfo(
            account_id=self._user,
            balance=0.0,
            equity=0.0,
            daily_pnl=0.0,
            eod_threshold=0.0,
            buying_power=0.0,
            open_positions_count=len(self._positions),
        )

    # ------------------------------------------------------------------
    # Event registration
    # ------------------------------------------------------------------

    def on_order_update(self, callback: OrderUpdateCallback) -> None:
        self._order_update_callbacks.append(callback)

    def on_connection_status_change(self, callback: ConnectionCallback) -> None:
        self._connection_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Internal event handlers
    # ------------------------------------------------------------------

    async def _handle_tick(self, tick_data: Any) -> None:
        """Translate async-rithmic tick event to our Tick model."""
        try:
            instrument = getattr(tick_data, "security_code", "")
            callback = self._tick_callbacks.get(instrument)
            if callback is None:
                return

            tick = Tick(
                instrument=instrument,
                timestamp=datetime.utcnow(),
                price=getattr(tick_data, "price", 0.0),
                size=getattr(tick_data, "size", 0),
                bid=getattr(tick_data, "bid_price", 0.0),
                ask=getattr(tick_data, "ask_price", 0.0),
                bid_size=getattr(tick_data, "bid_size", 0),
                ask_size=getattr(tick_data, "ask_size", 0),
            )
            await callback(tick)
        except Exception:
            logger.exception("Error handling tick data")

    async def _handle_order_book(self, ob_data: Any) -> None:
        """Translate async-rithmic order book event to our OrderBook model."""
        try:
            instrument = getattr(ob_data, "security_code", "")
            callback = self._order_book_callbacks.get(instrument)
            if callback is None:
                return

            bids = tuple(
                OrderBookLevel(
                    price=level.price, size=level.size,
                    order_count=getattr(level, "order_count", 0),
                )
                for level in getattr(ob_data, "bids", [])
            )
            asks = tuple(
                OrderBookLevel(
                    price=level.price, size=level.size,
                    order_count=getattr(level, "order_count", 0),
                )
                for level in getattr(ob_data, "asks", [])
            )
            order_book = OrderBook(
                instrument=instrument,
                timestamp=datetime.utcnow(),
                bids=bids,
                asks=asks,
            )
            await callback(order_book)
        except Exception:
            logger.exception("Error handling order book data")

    async def _handle_connected(self, *args: Any) -> None:
        self._connected = True
        await self._set_connection_status(ConnectionStatus.CONNECTED)

    async def _handle_disconnected(self, *args: Any) -> None:
        self._connected = False
        await self._set_connection_status(ConnectionStatus.RECONNECTING)

    async def _set_connection_status(self, status: ConnectionStatus) -> None:
        self._connection_status = status
        for cb in self._connection_callbacks:
            try:
                await cb(status)
            except Exception:
                logger.exception("Error in connection status callback")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self._connected or self._client is None:
            raise ConnectionError("Not connected to Rithmic")

    @staticmethod
    def _resolve_exchange(instrument: str) -> str:
        """Map instrument symbols to their exchange."""
        symbol = instrument.upper().rstrip("0123456789")
        exchange_map = {
            "ES": "CME",
            "MES": "CME",
            "NQ": "CME",
            "MNQ": "CME",
        }
        return exchange_map.get(symbol, "CME")
