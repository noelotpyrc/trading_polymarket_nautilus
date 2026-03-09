"""Unit tests for live strategy pure logic."""
import pytest

from live.strategies.btc_updown import compute_signal


class TestComputeSignal:
    def test_bullish(self):
        # Last close > first close → bullish
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        assert compute_signal(closes) == 1

    def test_bearish(self):
        # Last close < first close → bearish
        closes = [105.0, 104.0, 103.0, 102.0, 101.0, 100.0]
        assert compute_signal(closes) == -1

    def test_neutral_flat(self):
        # Last == first → neutral (regardless of middle values)
        closes = [100.0, 110.0, 90.0, 100.0]
        assert compute_signal(closes) == 0

    def test_two_element_up(self):
        assert compute_signal([99.0, 100.0]) == 1

    def test_two_element_down(self):
        assert compute_signal([100.0, 99.0]) == -1

    def test_single_element_returns_neutral(self):
        assert compute_signal([100.0]) == 0

    def test_empty_returns_neutral(self):
        assert compute_signal([]) == 0

    def test_only_last_and_first_matter(self):
        # Middle values are irrelevant to the signal
        assert compute_signal([100.0, 50.0, 200.0, 150.0, 101.0]) == 1
        assert compute_signal([100.0, 50.0, 200.0, 150.0, 99.0]) == -1
