from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.status import _downsample_snapshots
from models.models import PortfolioSnapshot


def test_downsample_snapshots_spans_full_time_range() -> None:
    start = datetime(2026, 3, 9, 0, 0, tzinfo=timezone.utc)
    snapshots = [
        PortfolioSnapshot(
            taken_at=start + timedelta(minutes=index),
            total_equity_usd=100.0 + index,
            available_cash_usd=50.0,
            exposure_usd=50.0,
            cumulative_pnl_usd=float(index),
        )
        for index in range(5000)
    ]

    sampled = _downsample_snapshots(snapshots, 2000)

    assert len(sampled) <= 2000
    assert sampled[0].taken_at == snapshots[0].taken_at
    assert sampled[-1].taken_at == snapshots[-1].taken_at
    assert sampled[0].taken_at < sampled[-1].taken_at


def test_downsample_snapshots_keeps_all_rows_when_under_limit() -> None:
    start = datetime(2026, 3, 9, 0, 0, tzinfo=timezone.utc)
    snapshots = [
        PortfolioSnapshot(
            taken_at=start + timedelta(minutes=index),
            total_equity_usd=100.0 + index,
            available_cash_usd=50.0,
            exposure_usd=50.0,
            cumulative_pnl_usd=float(index),
        )
        for index in range(10)
    ]

    sampled = _downsample_snapshots(snapshots, 2000)

    assert sampled == snapshots
