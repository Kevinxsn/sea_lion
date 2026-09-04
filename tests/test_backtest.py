import numpy as np

from conftest import synthetic_bars
from sea_lion.backtest import run_backtest, walk_forward_windows


def test_backtest_runs_and_respects_limits(cfg):
    bars = synthetic_bars(n_days=400, end=__import__("datetime").date(2026, 8, 28))
    dates = sorted(bars["date"].unique())
    res = run_backtest(cfg, bars, dates[120], dates[-1], initial_cash=2000.0, rebalance_days=5)
    assert len(res.equity) == len(dates) - 120
    assert np.isfinite(res.equity).all()
    assert (res.exposure <= cfg.risk.gross_exposure_max + 0.05).all()   # small drift from price moves
    assert res.n_rebalances > 0 and "total_return" in res.metrics
    assert res.equity.iloc[0] == 2000.0
    assert walk_forward_windows(dates)
