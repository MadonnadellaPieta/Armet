"""
Data models shared across the trading system.

These dataclasses define the core data structures used by the BrokerClient
interface and consumed by strategies, risk management, and the dashboard.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Direction(enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderType(enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(enum.Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ConnectionStatus(enum.Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"


class Phase(enum.Enum):
    EVAL = "EVAL"
    PA = "PA"


class SignalDecision(enum.Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ImpactLevel(enum.Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class CircuitBreakerTrigger(enum.Enum):
    MANUAL = "MANUAL"
    MAX_CONSECUTIVE_LOSSES = "MAX_CONSECUTIVE_LOSSES"
    MAX_DAILY_LOSS = "MAX_DAILY_LOSS"
    MAX_DAILY_PROFIT = "MAX_DAILY_PROFIT"
    DISCONNECTION = "DISCONNECTION"


# ---------------------------------------------------------------------------
# Market data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Tick:
    """A single price tick from the market data feed."""
    instrument: str
    timestamp: datetime
    price: float
    size: int
    bid: float
    ask: float
    bid_size: int
    ask_size: int


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    """A single price level in the order book."""
    price: float
    size: int
    order_count: int


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Level 2 order book snapshot."""
    instrument: str
    timestamp: datetime
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass(frozen=True, slots=True)
class Bar:
    """An OHLCV bar (candle)."""
    instrument: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    tick_count: int = 0


# ---------------------------------------------------------------------------
# Account & position models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Position:
    """An open position."""
    instrument: str
    direction: Direction
    quantity: int
    entry_price: float
    entry_timestamp: datetime
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    order_id: str = ""


@dataclass(slots=True)
class AccountInfo:
    """Account balance and status snapshot."""
    account_id: str
    balance: float
    equity: float
    daily_pnl: float
    eod_threshold: float
    buying_power: float
    open_positions_count: int
    timestamp: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Order models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BracketOrderRequest:
    """Request to place a bracket order (entry + SL + TP)."""
    instrument: str
    direction: Direction
    quantity: int
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    order_type: OrderType = OrderType.MARKET


@dataclass(slots=True)
class OrderUpdate:
    """Update on an order's status."""
    order_id: str
    status: OrderStatus
    instrument: str
    direction: Direction
    quantity: int
    filled_quantity: int = 0
    fill_price: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    message: str = ""
    parent_order_id: str = ""


@dataclass(slots=True)
class BracketOrderResult:
    """Result from placing a bracket order — contains IDs for all three legs."""
    entry_order_id: str
    stop_loss_order_id: str
    take_profit_order_id: str
    status: OrderStatus = OrderStatus.PENDING


# ---------------------------------------------------------------------------
# Signal models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Signal:
    """A trading signal generated by a strategy."""
    id: str
    instrument: str
    strategy_name: str
    direction: Direction
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    rr_ratio: float
    confidence_score: float
    confluence_factors: dict[str, Any] = field(default_factory=dict)
    indicator_state: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    expiry_seconds: float = 10.0
    decision: SignalDecision | None = None
    decision_timestamp: datetime | None = None
