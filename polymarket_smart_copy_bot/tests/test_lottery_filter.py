"""Tests for the lottery trader (median vs mean) PnL consistency filter."""

from __future__ import annotations

import pytest
from statistics import median

from core.wallet_scorer import WalletScorer


class TestLotteryFilter:
    """Verify the median-vs-mean PnL consistency check inside _qualified_reason."""

    # Shared baseline params that pass all other checks
    BASE = dict(
        win_rate=0.70,
        trade_count_30d=40,
        trade_count_90d=120,
        profit_factor=1.8,
        avg_position_size=500.0,
        total_volume_90d=60_000.0,
        account_age_days=180,
        consecutive_losses=2,
        score=0.65,
    )

    # ── 1. Lottery trader → rejected ─────────────────────────────────────
    def test_lottery_trader_rejected(self):
        """80 losses + 20 big wins → median < 0, mean > 0 → 'lottery_trader_median_negative'."""
        pnl = [-100.0] * 80 + [5_000.0] * 20  # median=-100, mean=+920
        assert median(pnl) < 0
        assert sum(pnl) / len(pnl) > 0

        qualified, reason = WalletScorer._qualified_reason(
            **self.BASE,
            pnl_values_90d=pnl,
        )
        assert not qualified
        assert reason == "lottery_trader_median_negative"

    # ── 2. Consistent trader → passes ────────────────────────────────────
    def test_consistent_trader_passes(self):
        """70 wins + 30 small losses → median > 0 → passes."""
        pnl = [200.0] * 70 + [-50.0] * 30  # median=+200, mean=+125
        assert median(pnl) > 0

        qualified, reason = WalletScorer._qualified_reason(
            **self.BASE,
            pnl_values_90d=pnl,
        )
        assert qualified
        assert reason == "qualified"

    # ── 3. Too few trades with PnL → skip check ─────────────────────────
    def test_too_few_pnl_trades_skipped(self):
        """Only 5 trades with PnL → filter not applied → passes."""
        pnl = [-100.0] * 4 + [5_000.0]  # just 5 items, would be lottery if checked
        assert len(pnl) < 10

        qualified, reason = WalletScorer._qualified_reason(
            **self.BASE,
            pnl_values_90d=pnl,
        )
        assert qualified
        assert reason == "qualified"

    # ── 4. Both negative (not lottery) → caught by other filters ─────────
    def test_both_negative_not_flagged_as_lottery(self):
        """median < 0 and mean < 0 → not a lottery pattern, passes this filter."""
        pnl = [-100.0] * 15  # median=-100, mean=-100 — not lottery
        assert median(pnl) < 0
        assert sum(pnl) / len(pnl) < 0

        # This would be caught by other filters (low score etc.), not by this one
        qualified, reason = WalletScorer._qualified_reason(
            **self.BASE,
            pnl_values_90d=pnl,
        )
        # Should NOT be reason="lottery_trader_median_negative"
        assert reason != "lottery_trader_median_negative"

    # ── 5. No PnL data at all → skip check ──────────────────────────────
    def test_no_pnl_data_passes(self):
        """None/empty pnl_values_90d → filter skipped, passes."""
        qualified, reason = WalletScorer._qualified_reason(
            **self.BASE,
            pnl_values_90d=None,
        )
        assert qualified
        assert reason == "qualified"

        qualified2, reason2 = WalletScorer._qualified_reason(
            **self.BASE,
            pnl_values_90d=[],
        )
        assert qualified2
        assert reason2 == "qualified"
