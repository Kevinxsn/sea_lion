import numpy as np
import pandas as pd

from conftest import synthetic_bars
from sea_lion import features as F
from sea_lion.config import FeatureCfg, RegimeCfg


def test_no_lookahead():
    """Changing bars after t must not change features at t."""
    cfg = FeatureCfg()
    bars = synthetic_bars(n_days=200)
    dates = sorted(bars["date"].unique())
    t = dates[150]
    fp1 = F.compute_feature_panel(F.Panel.from_long(bars), cfg)
    f1 = F.features_at(fp1, t)
    tampered = bars.copy()
    tampered.loc[tampered["date"] > t, ["close", "adj_close"]] *= 3.0
    fp2 = F.compute_feature_panel(F.Panel.from_long(tampered), cfg)
    f2 = F.features_at(fp2, t)
    for c in ["mom_short", "mom_long", "trend", "vol", "dollar_volume", "volume_ratio"]:
        pd.testing.assert_series_equal(f1[c], f2[c], check_names=False)


def test_momentum_skips_last_day():
    cfg = FeatureCfg(mom_short=20, skip_last_day=1)
    bars = synthetic_bars(symbols=["AAPL", "SPY"], n_days=100)
    fp = F.compute_feature_panel(F.Panel.from_long(bars), cfg)
    ac = fp["adj_close"]["AAPL"]
    t = ac.index[-1]
    expected = ac.iloc[-2] / ac.iloc[-22] - 1
    assert abs(fp["mom_short"].loc[t, "AAPL"] - expected) < 1e-12


def test_regime_states():
    df = pd.DataFrame({"trend": [0.05], "vol": [0.12]}, index=["SPY"])
    assert F.market_regime(df, "SPY", RegimeCfg())["state"] == "bull"
    df = pd.DataFrame({"trend": [-0.05], "vol": [0.40]}, index=["SPY"])
    assert F.market_regime(df, "SPY", RegimeCfg())["state"] == "bear"
    df = pd.DataFrame({"trend": [0.05], "vol": [0.40]}, index=["SPY"])
    assert F.market_regime(df, "SPY", RegimeCfg())["state"] == "neutral"
    df = pd.DataFrame({"trend": [np.nan], "vol": [0.1]}, index=["SPY"])
    r = F.market_regime(df, "SPY", RegimeCfg())
    assert r["state"] == "unknown" and r["multiplier"] == RegimeCfg().bear


def test_winsorize_and_zscore():
    s = pd.Series([1.0, 2.0, 3.0, 100.0])
    w = F.winsorize(s, 0.25)
    assert w.max() < 100
    z = F.zscore(w)
    assert abs(z.mean()) < 1e-12
    assert F.zscore(pd.Series([1.0, 1.0, 1.0])).abs().sum() == 0
