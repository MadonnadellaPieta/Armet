"""
Signal Engine Orchestrator.

Central pipeline that ties together strategies, confluence filters, the
confidence scorer, and the risk manager into a single, linear processing
pipeline.

Flow (per bar or tick)
----------------------
1. Fan out to all *enabled* strategies whose instrument matches.
2. For each raw Signal returned, evaluate all three confluence filters.
3. Feed filter results into ConfidenceScorer → (confidence_score, factors).
4. Enrich the Signal with the computed score and confluence_factors.
5. Run the enriched Signal through RiskManager with current account/positions.
6. If approved  → call signal_callback, optionally persist to DB, return Signal.
   If rejected  → persist with rejection reason, return None.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.models import (
    AccountInfo,
    Bar,
    Position,
    Signal,
    SignalDecision,
    Tick,
)
from backend.database.models import SignalRecord
from backend.filters.market_profile import MarketProfileFilter
from backend.filters.order_flow import OrderFlowFilter
from backend.filters.support_resistance import SupportResistanceFilter
from backend.services.confidence_scorer import ConfidenceScorer
from backend.services.risk_manager import RiskManager
from backend.strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)

# Callback invoked with the approved signal and the final approved quantity.
SignalCallback = Callable[[Signal, int], Awaitable[None]]


class SignalEngine:
    """Orchestrates the full signal generation and validation pipeline.

    Parameters
    ----------
    strategies:
        List of strategy instances.  The engine fans out to each enabled
        strategy whose ``instrument`` matches the incoming bar/tick.
    order_flow_filter:
        Stateful filter that classifies buy/sell pressure from tick data.
    market_profile_filter:
        Filter based on current-session value area (POC/VAH/VAL).
    support_resistance_filter:
        Filter based on prior session high/low and key price levels.
    scorer:
        Aggregates the three filter outputs into a 0–1 confidence score.
    risk_manager:
        Enforces Apex guardrails and R:R constraints.
    signal_callback:
        Async callable invoked with each *approved* signal.
        Signature: ``async def cb(signal: Signal, quantity: int) -> None``.
    session_factory:
        SQLAlchemy async session factory used to persist signals.
        If ``None``, persistence is skipped (useful in unit tests).
    requested_quantity:
        Default contract quantity forwarded to the risk manager.
        The risk manager may reduce this if limits are tight.
    """

    def __init__(
        self,
        strategies: list[BaseStrategy],
        order_flow_filter: OrderFlowFilter,
        market_profile_filter: MarketProfileFilter,
        support_resistance_filter: SupportResistanceFilter,
        scorer: ConfidenceScorer,
        risk_manager: RiskManager,
        signal_callback: SignalCallback | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        requested_quantity: int = 1,
    ) -> None:
        self._strategies = list(strategies)
        self._filters: dict[str, Any] = {
            "order_flow": order_flow_filter,
            "market_profile": market_profile_filter,
            "support_resistance": support_resistance_filter,
        }
        self._scorer = scorer
        self._risk_manager = risk_manager
        self._signal_callback = signal_callback
        self._session_factory = session_factory
        self._requested_quantity = requested_quantity

        # Runtime state — updated via set_account / set_positions
        self._account: AccountInfo | None = None
        self._positions: list[Position] = []

    # ------------------------------------------------------------------
    # Runtime state setters
    # ------------------------------------------------------------------

    def set_account(self, account: AccountInfo) -> None:
        """Update the current account snapshot used for risk checks."""
        self._account = account

    def set_positions(self, positions: list[Position]) -> None:
        """Update the list of open positions used for risk checks."""
        self._positions = list(positions)

    # ------------------------------------------------------------------
    # Market data entry points
    # ------------------------------------------------------------------

    async def on_bar(self, bar: Bar) -> list[Signal]:
        """Process a completed bar through all enabled, matching strategies.

        Returns the list of signals that were approved by the risk manager.
        Rejected signals are persisted with their rejection reason but are
        not returned.
        """
        approved: list[Signal] = []
        for strategy in self._strategies:
            if not strategy.enabled:
                continue
            if strategy.instrument != bar.instrument:
                continue
            raw_signal = strategy.on_bar(bar)
            if raw_signal is None:
                continue
            result = await self._process_signal(raw_signal)
            if result is not None:
                approved.append(result)
        return approved

    async def on_tick(self, tick: Tick) -> list[Signal]:
        """Process a raw tick.

        Always feeds the tick to the ``OrderFlowFilter`` so delta state
        accumulates.  Then fans out to any tick-aware strategies.

        Returns the list of approved signals (most strategies ignore ticks,
        so this is usually empty).
        """
        self._filters["order_flow"].on_tick(tick)

        approved: list[Signal] = []
        for strategy in self._strategies:
            if not strategy.enabled:
                continue
            if strategy.instrument != tick.instrument:
                continue
            raw_signal = strategy.on_tick(tick)
            if raw_signal is None:
                continue
            result = await self._process_signal(raw_signal)
            if result is not None:
                approved.append(result)
        return approved

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    async def _process_signal(self, raw_signal: Signal) -> Signal | None:
        """Run the full filter → score → risk pipeline on *raw_signal*.

        Returns the enriched, approved Signal, or ``None`` if rejected.
        Both approved and rejected signals are persisted when a session
        factory is configured.
        """
        if self._account is None:
            logger.warning(
                "Signal from %s discarded: no account info has been set.",
                raw_signal.strategy_name,
            )
            return None

        # ---- Step 1: run all confluence filters -------------------------
        filter_results: dict[str, Any] = {}
        for name, filt in self._filters.items():
            filter_results[name] = filt.evaluate(
                raw_signal.direction, raw_signal.entry_price
            )

        # ---- Step 2: score confidence -----------------------------------
        confidence_score, confluence_factors = self._scorer.score(filter_results)

        # ---- Step 3: enrich signal with score ---------------------------
        enriched = dataclasses.replace(
            raw_signal,
            confidence_score=confidence_score,
            confluence_factors=confluence_factors,
        )

        # ---- Step 4: risk validation ------------------------------------
        risk_result = self._risk_manager.validate(
            enriched,
            self._account,
            self._positions,
            self._requested_quantity,
        )

        now = datetime.utcnow()

        if risk_result.approved:
            approved_qty = risk_result.adjusted_quantity or self._requested_quantity
            final_signal = dataclasses.replace(
                enriched,
                decision=SignalDecision.ACCEPTED,
                decision_timestamp=now,
            )
            await self._persist_signal(final_signal)
            if self._signal_callback is not None:
                await self._signal_callback(final_signal, approved_qty)
            logger.info(
                "Signal ACCEPTED: strategy=%s instrument=%s direction=%s "
                "qty=%d confidence=%.3f rr=%.2f",
                final_signal.strategy_name,
                final_signal.instrument,
                final_signal.direction.value,
                approved_qty,
                final_signal.confidence_score,
                final_signal.rr_ratio,
            )
            return final_signal

        # Rejected — embed the reason in indicator_state for traceability
        rejected_state = {
            **enriched.indicator_state,
            "rejection_reason": risk_result.reason,
        }
        rejected = dataclasses.replace(
            enriched,
            indicator_state=rejected_state,
            decision=SignalDecision.REJECTED,
            decision_timestamp=now,
        )
        await self._persist_signal(rejected)
        logger.info(
            "Signal REJECTED: strategy=%s instrument=%s — %s",
            rejected.strategy_name,
            rejected.instrument,
            risk_result.reason,
        )
        return None

    async def _persist_signal(self, signal: Signal) -> None:
        """Write *signal* to the database.  Silently skips if no factory set."""
        if self._session_factory is None:
            return
        try:
            async with self._session_factory() as session:
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
                    decision=signal.decision.value if signal.decision else None,
                    decision_timestamp=signal.decision_timestamp,
                )
                session.add(record)
                await session.commit()
        except Exception:
            logger.exception("Failed to persist signal %s", signal.id)
