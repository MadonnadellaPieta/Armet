"""
Tests for the ConfidenceScorer service.

Verifies score aggregation math, clamping, weight customization,
missing-filter handling, and confluence_factors structure.
"""

from __future__ import annotations

import pytest

from backend.services.confidence_scorer import ConfidenceScorer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result(adjustment: float, aligned: bool = True) -> dict:
    return {
        "confidence_adjustment": adjustment,
        "aligned": aligned,
        "details": {"some_key": "some_value"},
    }


def _all_filters(adjustment: float) -> dict:
    return {
        "order_flow": _result(adjustment),
        "market_profile": _result(adjustment),
        "support_resistance": _result(adjustment),
    }


# ===========================================================================
# ConfidenceScorer Tests
# ===========================================================================

class TestConfidenceScorer:
    def test_all_neutral_returns_half(self):
        scorer = ConfidenceScorer()
        score, _ = scorer.score(_all_filters(0.0))
        assert score == pytest.approx(0.5)

    def test_all_aligned_boosts_score(self):
        scorer = ConfidenceScorer()
        score, _ = scorer.score(_all_filters(1.0))
        # 0.5 + (1.0 * 0.25) * 3 = 0.5 + 0.75 = 1.25 → clamped to 1.0
        assert score == pytest.approx(1.0)

    def test_all_divergent_reduces_score(self):
        scorer = ConfidenceScorer()
        score, _ = scorer.score(_all_filters(-1.0))
        # 0.5 + (-1.0 * 0.25) * 3 = 0.5 - 0.75 = -0.25 → clamped to 0.0
        assert score == pytest.approx(0.0)

    def test_single_filter_provided(self):
        """Only one filter result; others missing → neutral contribution."""
        scorer = ConfidenceScorer()
        score, _ = scorer.score({"order_flow": _result(1.0)})
        # 0.5 + 1.0 * 0.25 = 0.75 (market_profile + support_resistance contribute 0)
        assert score == pytest.approx(0.75)

    def test_empty_filter_results_returns_base(self):
        scorer = ConfidenceScorer()
        score, _ = scorer.score({})
        assert score == pytest.approx(0.5)

    def test_custom_weights_proportional(self):
        scorer = ConfidenceScorer({
            "weights": {
                "order_flow": 0.5,
                "market_profile": 0.0,
                "support_resistance": 0.0,
            }
        })
        score, _ = scorer.score({"order_flow": _result(1.0)})
        # 0.5 + 1.0 * 0.5 = 1.0
        assert score == pytest.approx(1.0)

    def test_score_clamped_to_one(self):
        scorer = ConfidenceScorer({
            "weights": {
                "order_flow": 1.0,
                "market_profile": 1.0,
                "support_resistance": 1.0,
            }
        })
        score, _ = scorer.score(_all_filters(1.0))
        assert score == pytest.approx(1.0)

    def test_score_clamped_to_zero(self):
        scorer = ConfidenceScorer({
            "weights": {
                "order_flow": 1.0,
                "market_profile": 1.0,
                "support_resistance": 1.0,
            }
        })
        score, _ = scorer.score(_all_filters(-1.0))
        assert score == pytest.approx(0.0)

    def test_confluence_factors_structure(self):
        scorer = ConfidenceScorer()
        score, factors = scorer.score(_all_filters(0.5))

        # Top-level metadata keys
        assert "weights" in factors
        assert "base_score" in factors
        assert "final_score" in factors
        assert factors["final_score"] == score

        # Each filter block
        for name in ("order_flow", "market_profile", "support_resistance"):
            assert name in factors
            block = factors[name]
            assert "confidence_adjustment" in block
            assert "aligned" in block
            assert "details" in block
            assert "weight" in block
            assert "weighted_contribution" in block

    def test_missing_filter_has_neutral_block(self):
        """A filter not in filter_results still appears in confluence_factors."""
        scorer = ConfidenceScorer()
        _, factors = scorer.score({"order_flow": _result(0.8)})

        # market_profile was not provided
        mp = factors["market_profile"]
        assert mp["confidence_adjustment"] == 0.0
        assert mp["aligned"] is False
        assert mp["weighted_contribution"] == 0.0

    def test_partial_alignment_between_half_and_max(self):
        scorer = ConfidenceScorer()
        # One aligned, two neutral → score > 0.5 but < 1.0
        score, _ = scorer.score({
            "order_flow": _result(0.8),
            "market_profile": _result(0.0),
            "support_resistance": _result(0.0),
        })
        assert 0.5 < score < 1.0

    def test_higher_weight_yields_larger_shift(self):
        low_weight_scorer = ConfidenceScorer({
            "weights": {"order_flow": 0.1, "market_profile": 0.0, "support_resistance": 0.0}
        })
        high_weight_scorer = ConfidenceScorer({
            "weights": {"order_flow": 0.4, "market_profile": 0.0, "support_resistance": 0.0}
        })
        result = {"order_flow": _result(1.0)}
        low_score, _ = low_weight_scorer.score(result)
        high_score, _ = high_weight_scorer.score(result)
        assert high_score > low_score

    def test_weighted_contribution_stored_correctly(self):
        scorer = ConfidenceScorer()
        _, factors = scorer.score({"order_flow": _result(0.8)})
        expected = round(0.8 * 0.25, 4)
        assert factors["order_flow"]["weighted_contribution"] == pytest.approx(expected)

    def test_score_symmetric_around_half(self):
        """A fully aligned and fully divergent single filter are equidistant from 0.5."""
        scorer = ConfidenceScorer()
        up, _ = scorer.score({"order_flow": _result(1.0)})
        down, _ = scorer.score({"order_flow": _result(-1.0)})
        assert abs((up - 0.5) - (0.5 - down)) < 1e-9
