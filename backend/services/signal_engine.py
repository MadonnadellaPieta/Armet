"""
Signal Engine — orchestrates the full signal generation pipeline.

Pipeline per bar/tick:
    1. Check session manager (trading hours guard)
    2. Check circuit breaker (kill-switch guard)
    3. Fan out to enabled strategies
    4. For each emitted Signal:
       a. Run all enabled confluence filters
       b. Score confidence via ConfidenceScorer
       c. Validate via RiskManager
       d. Persist SignalRecord (approved or rejected)
       e. Log EventRecord on risk rejection
    5. Return approved (signal, quantity) pairs to caller

The engine does NOT place orders — that is the responsibility of the
caller (typically the OrderManager).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.broker_client import BrokerClient
from backend.core.models import Bar, Signal, SignalDecision, Tick
from backend.database.models import EventRecord, SignalRecord
from backend.filters.market_profile import MarketProfileFilter
from backend.filters.order_flow import OrderFlowFilter
from backend.filters.support_resistance import SupportResistanceFilter
from backend.services.circuit_breaker import CircuitBreaker
from backend.services.confidence_scorer import ConfidenceScorer
from backend.services.risk_manager import RiskCheckResult, RiskManager
from backend.services.session_manager import SessionManager
from backend.strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


def _uid() -> str:
    return uuid.uuid4().hex


@dataclass
class ApprovedSignal:
    """A signal that passed all risk checks, ready for order placement."""
    signal: Signal
    approved_quantity: int
    risk_details: dict[str, Any]


class SignalEngine:
    """Orchestrates strategies → filters → confidence → risk into approved signals."""

    def __init__(
        self,
        strategies: list[BaseStrategy],
        broker: BrokerClient,
        session_factory: async_sessionmaker[AsyncSession],
        filters: dict[str, Any] | None = None,
        scorer: ConfidenceScorer | None = None,
        risk_manager: RiskManager | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        session_manager: SessionManager | None = None,
    ) -> None:
        self._strategies = strategies
        self._broker = broker
        self._session_factory = session_factory

        # Filters default to neutral instances (no data) — still run pipeline
        self._filters: dict[str, Any] = filters or {
            "order_flow": OrderFlowFilter(),
            "market_profile": MarketProfileFilter(),
            "support_resistance": SupportResistanceFilter(),
        }

        self._scorer = scorer or ConfidenceScorer()
        self._risk_manager = risk_manager or RiskManager()
        self._circuit_breaker = circuit_breaker
        self._session_manager = session_manager

    # ------------------------------------------------------------------
    # Market data ingestion
    # ------------------------------------------------------------------

    async def on_bar(self, bar: Bar) -> list[ApprovedSignal]:
        """Process a bar through all enabled strategies.

        Returns approved signals ready for order placement.
        """
        if not self._is_active():
            return []

        # Feed bar to filters that support it
        for f in self._filters.values():
            if hasattr(f, "on_bar"):
                f.on_bar(bar)

        approved: list[ApprovedSignal] = []
        for strategy in self._strategies:
            if not strategy.enabled:
                continue
            signal = strategy.on_bar(bar)
            if signal is None:
                continue
            result = await self._process_signal(signal)
            if result is not None:
                approved.append(result)

        return approved

    async def on_tick(self, tick: Tick) -> list[ApprovedSignal]:
        """Process a tick through strategies that implement on_tick."""
        if not self._is_active():
            return []

        # Feed tick to filters
        for f in self._filters.values():
            if hasattr(f, "on_tick"):
                f.on_tick(tick)

        approved: list[ApprovedSignal] = []
        for strategy in self._strategies:
            if not strategy.enabled:
                continue
            signal = strategy.on_tick(tick)
            if signal is None:
                continue
            result = await self._process_signal(signal)
            if result is not None:
                approved.append(result)

        return approved

    # ------------------------------------------------------------------
    # Signal processing pipeline
    # ------------------------------------------------------------------

    async def _process_signal(self, signal: Signal) -> ApprovedSignal | None:
        """Run a raw strategy signal through filters → confidence → risk.

        Persists a SignalRecord regardless of outcome.
        Returns ApprovedSignal if the signal passes all checks, else None.
        """
        # 1. Run confluence filters
        filter_results: dict[str, dict] = {}
        for name, f in self._filters.items():
            try:
                filter_results[name] = f.evaluate(signal.direction, signal.entry_price)
            except Exception:
                logger.exception("Filter %s raised on evaluate()", name)
                filter_results[name] = {
                    "aligned": False,
                    "confidence_adjustment": 0.0,
                    "details": {},
                }

        # 2. Score confidence
        score, factors = self._scorer.score(filter_results)
        signal.confidence_score = score
        signal.confluence_factors = factors

        # 3. Risk validation
        account = await self._broker.get_account_info()
        positions = await self._broker.get_positions()
        risk_result: RiskCheckResult = self._risk_manager.validate(
            signal, account, positions
        )

        # 4. Persist signal + optionally log risk-rejection event
        await self._persist_signal(signal, risk_result)

        if not risk_result.approved:
            logger.info(
                "Signal %s blocked by risk manager: %s",
                signal.id, risk_result.reason,
            )
            return None

        return ApprovedSignal(
            signal=signal,
            approved_quantity=risk_result.adjusted_quantity,
            risk_details=risk_result.details,
        )

    async def _persist_signal(
        self, signal: Signal, risk_result: RiskCheckResult
    ) -> None:
        """Write SignalRecord (and EventRecord on risk rejection) to DB."""
        now = datetime.utcnow()
        async with self._session_factory() as sess:
            record = SignalRecord(
                id=signal.id,
                timestamp=signal.timestamp,
                instrument=signal.instrument,
                strategy_name=signal.strategy_name,
                direction=signal.direction.value,
                entry_price=signal.entry_price,
                sl_price=signal.stop_loss_price,
                tp_price=signal.take_profit_price,
                rr_ratio=signal.rr_ratio,
                confidence_score=signal.confidence_score,
                confluence_factors=signal.confluence_factors,
                indicator_state=signal.indicator_state,
            )

            if not risk_result.approved:
                record.decision = SignalDecision.REJECTED.value
                record.decision_timestamp = now

                # Log rejection as an EventRecord so tests can find it
                event = EventRecord(
                    id=_uid(),
                    timestamp=now,
                    name=f"Risk rejection [{signal.strategy_name}]: {risk_result.reason}",
                    impact_level="LOW",
                )
                sess.add(event)

            sess.add(record)
            await sess.commit()

    # ------------------------------------------------------------------
    # Guard helpers
    # ------------------------------------------------------------------

    def _is_active(self) -> bool:
        """Return False if the circuit breaker is tripped or outside hours."""
        if self._circuit_breaker and self._circuit_breaker.is_tripped:
            return False
        if self._session_manager and self._session_manager.should_suppress_signal():
            return False
        return True
