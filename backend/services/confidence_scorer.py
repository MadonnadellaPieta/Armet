"""
Confidence Scorer.

Aggregates the three confluence filter results into a single confidence score
(0.0–1.0) for display on the dashboard and for signal ranking.

Formula:
    base_score = 0.5  (neutral)
    score = clamp(base_score + sum(adjustment * weight), 0.0, 1.0)

Each filter contributes its confidence_adjustment (in [-1.0, 1.0]) multiplied
by its configured weight (default 0.25).  Missing or disabled filters
contribute 0.0 so the score degrades gracefully.
"""

from __future__ import annotations

from typing import Any


_FILTER_NAMES = ("order_flow", "market_profile", "support_resistance")
_DEFAULT_WEIGHT = 0.25


class ConfidenceScorer:
    """Aggregate confluence filter outputs into a single confidence score."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        defaults = self.default_params()
        self._params = {**defaults, **(params or {})}

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "weights": {
                "order_flow": _DEFAULT_WEIGHT,
                "market_profile": _DEFAULT_WEIGHT,
                "support_resistance": _DEFAULT_WEIGHT,
            },
            "base_score": 0.5,
        }

    def update_params(self, new_params: dict[str, Any]) -> None:
        for k, v in new_params.items():
            if k in self._params:
                self._params[k] = v

    def score(
        self, filter_results: dict[str, dict[str, Any]]
    ) -> tuple[float, dict[str, Any]]:
        """Compute the confidence score from filter evaluation results.

        Args:
            filter_results: Mapping of filter name → evaluate() output dict.
                Each dict must have a "confidence_adjustment" key in [-1.0, 1.0].
                Missing filters are treated as neutral (0.0 adjustment).

        Returns:
            (confidence_score, confluence_factors)
            - confidence_score: float in [0.0, 1.0]
            - confluence_factors: structured breakdown for storage / display
        """
        weights: dict[str, float] = self._params["weights"]
        base: float = self._params["base_score"]

        total_adjustment = 0.0
        factors: dict[str, Any] = {}

        for name in _FILTER_NAMES:
            weight = weights.get(name, _DEFAULT_WEIGHT)
            result = filter_results.get(name)

            if result is not None:
                adjustment = float(result.get("confidence_adjustment", 0.0))
                aligned = result.get("aligned", False)
                details = result.get("details", {})
            else:
                adjustment = 0.0
                aligned = False
                details = {}

            total_adjustment += adjustment * weight
            factors[name] = {
                "confidence_adjustment": adjustment,
                "aligned": aligned,
                "details": details,
                "weight": weight,
                "weighted_contribution": round(adjustment * weight, 4),
            }

        raw_score = base + total_adjustment
        final_score = round(max(0.0, min(1.0, raw_score)), 4)

        factors["weights"] = dict(weights)
        factors["base_score"] = base
        factors["final_score"] = final_score

        return final_score, factors
