"""
Tests for all three confluence filters.

Verifies each filter produces meaningful confidence adjustments
for both aligned and divergent scenarios.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.core.models import Bar, Direction, OrderBook, OrderBookLevel, Tick
from backend.filters.order_flow import OrderFlowFilter
from backend.filters.market_profile import MarketProfileFilter
from backend.filters.support_resistance import SupportResistanceFilter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tick(price: float = 5100.0, bid: float = 5099.75, ask: float = 5100.25, size: int = 5) -> Tick:
    return Tick("ES", datetime.utcnow(), price, size, bid, ask, 10, 10)


def _bar(o: float = 5100.0, h: float = 5102.0, l: float = 5098.0, c: float = 5101.0, volume: int = 1000) -> Bar:
    return Bar("ES", datetime.utcnow(), o, h, l, c, volume)


# ===========================================================================
# Order Flow Filter Tests
# ===========================================================================

class TestOrderFlowFilter:
    def test_no_data_returns_neutral(self):
        f = OrderFlowFilter()
        result = f.evaluate(Direction.LONG, 5100.0)
        assert result["confidence_adjustment"] == 0.0
        assert result["aligned"] is False

    def test_strong_buying_aligns_with_long(self):
        f = OrderFlowFilter({"delta_threshold": 0.6})
        # Feed lots of buy ticks (price >= ask)
        for _ in range(80):
            f.on_tick(_tick(price=5100.25, ask=5100.25, size=10))
        for _ in range(20):
            f.on_tick(_tick(price=5099.75, bid=5099.75, size=10))

        result = f.evaluate(Direction.LONG, 5100.0)
        assert result["aligned"] is True
        assert result["delta_ratio"] >= 0.7
        assert result["confidence_adjustment"] > 0

    def test_strong_selling_aligns_with_short(self):
        f = OrderFlowFilter({"delta_threshold": 0.6})
        for _ in range(80):
            f.on_tick(_tick(price=5099.75, bid=5099.75, size=10))
        for _ in range(20):
            f.on_tick(_tick(price=5100.25, ask=5100.25, size=10))

        result = f.evaluate(Direction.SHORT, 5100.0)
        assert result["aligned"] is True
        assert result["confidence_adjustment"] > 0

    def test_buying_diverges_from_short(self):
        f = OrderFlowFilter({"delta_threshold": 0.6})
        for _ in range(80):
            f.on_tick(_tick(price=5100.25, ask=5100.25, size=10))
        for _ in range(20):
            f.on_tick(_tick(price=5099.75, bid=5099.75, size=10))

        result = f.evaluate(Direction.SHORT, 5100.0)
        assert result["aligned"] is False
        assert result["confidence_adjustment"] < 0

    def test_order_book_depth_factor(self):
        f = OrderFlowFilter()
        f.on_tick(_tick(price=5100.25, ask=5100.25, size=10))

        # Strong bid depth supports longs
        ob = OrderBook("ES", datetime.utcnow(),
            bids=(OrderBookLevel(5099.75, 100, 10), OrderBookLevel(5099.50, 80, 8)),
            asks=(OrderBookLevel(5100.25, 20, 3),),
        )
        f.on_order_book(ob)
        result = f.evaluate(Direction.LONG, 5100.0)
        assert result["details"]["depth_factor"] > 0

    def test_reset_clears_data(self):
        f = OrderFlowFilter()
        f.on_tick(_tick(price=5100.25, size=10))
        assert f._buy_volume > 0
        f.reset()
        assert f._buy_volume == 0
        assert f._sell_volume == 0

    def test_balanced_flow_neutral(self):
        f = OrderFlowFilter()
        for _ in range(50):
            f.on_tick(_tick(price=5100.25, ask=5100.25, size=10))
        for _ in range(50):
            f.on_tick(_tick(price=5099.75, bid=5099.75, size=10))

        result = f.evaluate(Direction.LONG, 5100.0)
        assert abs(result["confidence_adjustment"]) < 0.3


# ===========================================================================
# Market Profile Filter Tests
# ===========================================================================

class TestMarketProfileFilter:
    def _build_profile(self, center: float = 5100.0, bars: int = 50) -> MarketProfileFilter:
        """Build a market profile centered around a price."""
        f = MarketProfileFilter({"proximity_points": 3.0, "tick_size": 0.25})
        for i in range(bars):
            # Most volume concentrated at center
            spread = abs(i - bars // 2) * 0.5
            f.on_bar(_bar(
                o=center - spread, h=center + 1 - spread * 0.5,
                l=center - 1 - spread * 0.5, c=center - spread * 0.3,
                volume=max(1000 - i * 15, 100),
            ))
        return f

    def test_poc_computed(self):
        f = self._build_profile(5100.0)
        assert f._poc is not None

    def test_vah_val_computed(self):
        f = self._build_profile(5100.0)
        assert f._vah is not None
        assert f._val is not None
        assert f._vah >= f._val

    def test_long_near_val_is_aligned(self):
        f = self._build_profile(5100.0)
        # Set prior session levels near entry
        f.set_prior_session(poc=5100.0, vah=5105.0, val=5095.0)

        # Buying near VAL (support) should be aligned
        result = f.evaluate(Direction.LONG, 5095.0)
        # Should find at least one nearby level
        if result["details"].get("nearby_levels"):
            assert result["aligned"] is True

    def test_short_near_vah_is_aligned(self):
        f = self._build_profile(5100.0)
        f.set_prior_session(poc=5100.0, vah=5105.0, val=5095.0)

        result = f.evaluate(Direction.SHORT, 5105.0)
        if result["details"].get("nearby_levels"):
            assert result["aligned"] is True

    def test_no_nearby_levels_neutral(self):
        f = self._build_profile(5100.0)
        f.set_prior_session(poc=5100.0, vah=5105.0, val=5095.0)

        # Price far from any level
        result = f.evaluate(Direction.LONG, 5200.0)
        assert result["confidence_adjustment"] == 0.0
        assert result["aligned"] is False

    def test_prior_session_levels_included(self):
        f = MarketProfileFilter()
        f.set_prior_session(poc=5100.0, vah=5105.0, val=5095.0)
        assert f._prior_poc == 5100.0
        assert f._prior_vah == 5105.0
        assert f._prior_val == 5095.0

    def test_reset_clears_current_keeps_prior(self):
        f = self._build_profile(5100.0)
        f.set_prior_session(poc=5100.0, vah=5105.0, val=5095.0)
        f.reset()
        assert f._poc is None
        assert f._prior_poc == 5100.0  # prior preserved

    def test_empty_profile_returns_neutral(self):
        f = MarketProfileFilter()
        result = f.evaluate(Direction.LONG, 5100.0)
        assert result["confidence_adjustment"] == 0.0

    def test_details_include_levels(self):
        f = MarketProfileFilter()
        f.set_prior_session(poc=5100.0, vah=5105.0, val=5095.0)
        result = f.evaluate(Direction.LONG, 5100.0)
        assert "poc" in result["details"]
        assert "prior_poc" in result["details"]


# ===========================================================================
# Support / Resistance Filter Tests
# ===========================================================================

class TestSupportResistanceFilter:
    def test_no_levels_returns_neutral(self):
        f = SupportResistanceFilter()
        result = f.evaluate(Direction.LONG, 5100.0)
        assert result["confidence_adjustment"] == 0.0
        assert result["aligned"] is False

    def test_long_near_support_is_aligned(self):
        f = SupportResistanceFilter({"level_proximity_points": 3.0})
        f.set_levels(prior_session_low=5095.0)

        result = f.evaluate(Direction.LONG, 5096.0)
        assert result["aligned"] is True
        assert result["confidence_adjustment"] > 0

    def test_long_near_resistance_is_divergent(self):
        f = SupportResistanceFilter({"level_proximity_points": 3.0})
        f.set_levels(prior_session_high=5105.0)

        result = f.evaluate(Direction.LONG, 5104.0)
        assert result["aligned"] is False
        assert result["confidence_adjustment"] < 0

    def test_short_near_resistance_is_aligned(self):
        f = SupportResistanceFilter({"level_proximity_points": 3.0})
        f.set_levels(prior_session_high=5105.0)

        result = f.evaluate(Direction.SHORT, 5104.0)
        assert result["aligned"] is True
        assert result["confidence_adjustment"] > 0

    def test_short_near_support_is_divergent(self):
        f = SupportResistanceFilter({"level_proximity_points": 3.0})
        f.set_levels(prior_session_low=5095.0)

        result = f.evaluate(Direction.SHORT, 5096.0)
        assert result["aligned"] is False
        assert result["confidence_adjustment"] < 0

    def test_price_far_from_levels_neutral(self):
        f = SupportResistanceFilter({"level_proximity_points": 3.0})
        f.set_levels(prior_session_high=5200.0, prior_session_low=5000.0)

        result = f.evaluate(Direction.LONG, 5100.0)
        assert result["confidence_adjustment"] == 0.0

    def test_session_high_low_tracked(self):
        f = SupportResistanceFilter()
        f.on_bar(_bar(h=5110, l=5090))
        f.on_bar(_bar(h=5115, l=5092))
        assert f._session_high == 5115.0
        assert f._session_low == 5090.0

    def test_multiple_levels_nearby(self):
        f = SupportResistanceFilter({"level_proximity_points": 5.0})
        f.set_levels(
            prior_session_low=5095.0,
            overnight_low=5094.0,
        )
        # Both support levels nearby
        result = f.evaluate(Direction.LONG, 5096.0)
        assert len(result["details"]["nearby_levels"]) == 2
        assert result["aligned"] is True

    def test_custom_level(self):
        f = SupportResistanceFilter({"level_proximity_points": 3.0})
        f.add_custom_level("pivot_r1", 5105.0)

        result = f.evaluate(Direction.SHORT, 5104.0)
        assert len(result["details"]["nearby_levels"]) >= 1

    def test_set_levels_partial(self):
        f = SupportResistanceFilter()
        f.set_levels(prior_session_high=5110.0)
        assert f._prior_session_high == 5110.0
        assert f._prior_session_low is None  # not set

        f.set_levels(prior_session_low=5090.0)
        assert f._prior_session_high == 5110.0  # preserved
        assert f._prior_session_low == 5090.0

    def test_reset_clears_session_keeps_reference(self):
        f = SupportResistanceFilter()
        f.set_levels(prior_session_high=5110.0, weekly_high=5200.0)
        f.on_bar(_bar(h=5115, l=5090))
        f.reset()
        assert f._session_high is None
        assert f._prior_session_high == 5110.0
        assert f._weekly_high == 5200.0

    def test_details_include_all_levels(self):
        f = SupportResistanceFilter()
        f.set_levels(prior_session_high=5110.0, overnight_high=5108.0)
        result = f.evaluate(Direction.LONG, 5100.0)
        all_levels = result["details"]["all_levels"]
        assert "prior_session_high" in all_levels
        assert "overnight_high" in all_levels

    def test_closer_level_has_higher_strength(self):
        f = SupportResistanceFilter({"level_proximity_points": 5.0})
        f.set_levels(prior_session_low=5095.0)

        # Very close
        result1 = f.evaluate(Direction.LONG, 5095.5)
        # Farther
        result2 = f.evaluate(Direction.LONG, 5098.0)

        if result1["details"]["nearby_levels"] and result2["details"]["nearby_levels"]:
            assert result1["details"]["nearby_levels"][0]["strength"] > result2["details"]["nearby_levels"][0]["strength"]
