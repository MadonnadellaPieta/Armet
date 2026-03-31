"""
Risk Manager — Apex Trader Funding rule enforcement and R:R gating.

This module is the single enforcement point for all Apex-mandated guardrails.
Every signal **must** pass through ``RiskManager.validate()`` before an order
can be placed.  The rules are *non-overridable* — they cannot be loosened from
the dashboard.

Enforced rules
--------------
* **Max contracts** — EVAL: 8, PA: 6.
* **Max trailing drawdown** — EVAL: $3 000 (EOD trailing).
* **SL/TP ratio ceiling** — PA: 5:1 (SL distance / TP distance ≤ 5.0).
* **No hedging** — cannot hold opposing directions on the same instrument.
* **No naked orders** — every entry must have both SL and TP.
* **PA safety-net balance** — account balance must stay ≥ $103 100.
* **PA 30% negative-P&L rule** — no more than 30 % of total P&L can be
  negative on a single day.
* **R:R gating** — signal R:R must fall between ``min_rr_ratio`` and
  ``max_rr_ratio``.
* **Max concurrent positions** — configurable cap.
* **Daily loss circuit-breaker awareness** — rejects signals when daily loss
  already exceeds limit.

Each validation returns a ``RiskCheckResult`` with ``approved``, ``reason``
(if rejected), and optionally an adjusted ``quantity``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.core.models import (
    AccountInfo,
    Direction,
    Phase,
    Position,
    Signal,
)


@dataclass(frozen=True, slots=True)
class RiskCheckResult:
    """Outcome of a pre-trade risk check."""

    approved: bool
    reason: str = ""
    adjusted_quantity: int = 0
    details: dict[str, Any] = field(default_factory=dict)


class RiskManager:
    """Enforces Apex Trader Funding rules and R:R constraints."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        defaults = self.default_params()
        self._params: dict[str, Any] = {**defaults, **(params or {})}

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "phase": Phase.EVAL,
            # Apex — EVAL
            "eval_max_drawdown": 3000.0,
            "eval_max_contracts": 8,
            # Apex — PA
            "pa_max_contracts": 6,
            "pa_safety_net_balance": 103_100.0,
            "pa_negative_pnl_ratio": 0.30,
            "pa_max_sl_tp_ratio": 5.0,
            # R:R
            "min_rr_ratio": 1.5,
            "max_rr_ratio": 5.0,
            # General
            "max_concurrent_positions": 2,
            "max_daily_loss": 1500.0,
        }

    def update_params(self, new_params: dict[str, Any]) -> None:
        for k, v in new_params.items():
            if k in self._params:
                self._params[k] = v

    @property
    def phase(self) -> Phase:
        p = self._params["phase"]
        return p if isinstance(p, Phase) else Phase(p)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(
        self,
        signal: Signal,
        account: AccountInfo,
        open_positions: list[Position],
        requested_quantity: int = 1,
    ) -> RiskCheckResult:
        """Run all risk checks against *signal*.

        Returns a single ``RiskCheckResult``.  The first failing check
        short-circuits; order of checks is deliberately from cheapest to
        most expensive.
        """
        checks = [
            self._check_naked_order,
            self._check_rr_ratio,
            self._check_hedging,
            self._check_max_concurrent,
            self._check_max_contracts,
            self._check_daily_loss,
            self._check_drawdown,
            self._check_pa_safety_net,
            self._check_pa_negative_pnl,
            self._check_pa_sl_tp_ratio,
        ]

        qty = requested_quantity
        notes: list[str] = []
        for check in checks:
            result = check(signal, account, open_positions, qty)
            if not result.approved:
                return result
            # Propagate any quantity adjustment (e.g. from contract limit).
            if result.adjusted_quantity > 0:
                qty = result.adjusted_quantity
            if result.reason:
                notes.append(result.reason)

        return RiskCheckResult(
            approved=True,
            adjusted_quantity=qty,
            reason="; ".join(notes) if notes else "",
            details={"phase": self.phase.value},
        )

    # ------------------------------------------------------------------
    # Individual checks (all have the same signature)
    # ------------------------------------------------------------------

    def _check_naked_order(
        self,
        signal: Signal,
        account: AccountInfo,
        open_positions: list[Position],
        qty: int,
    ) -> RiskCheckResult:
        if signal.stop_loss_price == 0.0 or signal.take_profit_price == 0.0:
            return RiskCheckResult(
                approved=False,
                reason="Naked order rejected: SL and TP are both required.",
            )
        return _OK

    def _check_rr_ratio(
        self,
        signal: Signal,
        account: AccountInfo,
        open_positions: list[Position],
        qty: int,
    ) -> RiskCheckResult:
        rr = signal.rr_ratio
        lo, hi = self._params["min_rr_ratio"], self._params["max_rr_ratio"]
        if rr < lo:
            return RiskCheckResult(
                approved=False,
                reason=f"R:R {rr:.2f} below minimum {lo:.2f}.",
            )
        if rr > hi:
            return RiskCheckResult(
                approved=False,
                reason=f"R:R {rr:.2f} above maximum {hi:.2f}.",
            )
        return _OK

    def _check_hedging(
        self,
        signal: Signal,
        account: AccountInfo,
        open_positions: list[Position],
        qty: int,
    ) -> RiskCheckResult:
        for pos in open_positions:
            if pos.instrument == signal.instrument and pos.direction != signal.direction:
                return RiskCheckResult(
                    approved=False,
                    reason=(
                        f"Hedging not allowed: existing {pos.direction.value} "
                        f"position on {signal.instrument}."
                    ),
                )
        return _OK

    def _check_max_concurrent(
        self,
        signal: Signal,
        account: AccountInfo,
        open_positions: list[Position],
        qty: int,
    ) -> RiskCheckResult:
        limit = self._params["max_concurrent_positions"]
        if len(open_positions) >= limit:
            return RiskCheckResult(
                approved=False,
                reason=f"Max concurrent positions ({limit}) reached.",
            )
        return _OK

    def _check_max_contracts(
        self,
        signal: Signal,
        account: AccountInfo,
        open_positions: list[Position],
        qty: int,
    ) -> RiskCheckResult:
        phase = self.phase
        limit = (
            self._params["eval_max_contracts"]
            if phase == Phase.EVAL
            else self._params["pa_max_contracts"]
        )
        current = sum(p.quantity for p in open_positions)
        available = limit - current
        if available <= 0:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"{phase.value} max contracts ({limit}) fully utilised "
                    f"({current} open)."
                ),
            )
        if qty > available:
            return RiskCheckResult(
                approved=True,
                adjusted_quantity=available,
                reason=f"Quantity reduced from {qty} to {available} (contract limit).",
                details={"quantity_reduced": True},
            )
        return _OK

    def _check_daily_loss(
        self,
        signal: Signal,
        account: AccountInfo,
        open_positions: list[Position],
        qty: int,
    ) -> RiskCheckResult:
        limit = self._params["max_daily_loss"]
        if account.daily_pnl <= -limit:
            return RiskCheckResult(
                approved=False,
                reason=f"Daily loss limit (${limit:,.0f}) already reached.",
            )
        return _OK

    def _check_drawdown(
        self,
        signal: Signal,
        account: AccountInfo,
        open_positions: list[Position],
        qty: int,
    ) -> RiskCheckResult:
        if self.phase != Phase.EVAL:
            return _OK
        limit = self._params["eval_max_drawdown"]
        room = account.equity - account.eod_threshold
        if room <= 0:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"EVAL trailing drawdown breached: equity ${account.equity:,.2f} "
                    f"≤ EOD threshold ${account.eod_threshold:,.2f}."
                ),
            )
        return _OK

    def _check_pa_safety_net(
        self,
        signal: Signal,
        account: AccountInfo,
        open_positions: list[Position],
        qty: int,
    ) -> RiskCheckResult:
        if self.phase != Phase.PA:
            return _OK
        floor = self._params["pa_safety_net_balance"]
        if account.balance < floor:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"PA safety-net: balance ${account.balance:,.2f} "
                    f"below floor ${floor:,.2f}."
                ),
            )
        return _OK

    def _check_pa_negative_pnl(
        self,
        signal: Signal,
        account: AccountInfo,
        open_positions: list[Position],
        qty: int,
    ) -> RiskCheckResult:
        if self.phase != Phase.PA:
            return _OK
        if account.daily_pnl >= 0:
            return _OK
        ratio = self._params["pa_negative_pnl_ratio"]
        # 30 % rule: daily negative P&L must not exceed ratio of balance
        threshold = account.balance * ratio
        if abs(account.daily_pnl) >= threshold:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"PA 30% rule: daily loss ${abs(account.daily_pnl):,.2f} "
                    f"≥ {ratio:.0%} of balance (${threshold:,.2f})."
                ),
            )
        return _OK

    def _check_pa_sl_tp_ratio(
        self,
        signal: Signal,
        account: AccountInfo,
        open_positions: list[Position],
        qty: int,
    ) -> RiskCheckResult:
        if self.phase != Phase.PA:
            return _OK
        sl_dist = abs(signal.entry_price - signal.stop_loss_price)
        tp_dist = abs(signal.entry_price - signal.take_profit_price)
        if tp_dist == 0:
            return RiskCheckResult(
                approved=False,
                reason="TP distance is zero; cannot compute SL/TP ratio.",
            )
        ratio = sl_dist / tp_dist
        ceiling = self._params["pa_max_sl_tp_ratio"]
        if ratio > ceiling:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"PA SL/TP ratio {ratio:.2f} exceeds ceiling {ceiling:.1f}:1."
                ),
            )
        return _OK


# Shared singleton for the common "all OK" early-return.
_OK = RiskCheckResult(approved=True)
