"""Deterministic quantitative features, computed point-in-time.

Everything is a backward-looking rolling calculation over the price panel, so the row
for date `t` only uses bars <= t. Momentum additionally skips the most recent day.
Cross-sectional normalization (winsorize + z-score) happens *within one date* across
the universe, so it also cannot leak.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .config import FeatureCfg, RegimeCfg

FEATURE_COLS = ["mom_short", "mom_long", "trend", "above_trend", "vol", "dollar_volume", "volume_ratio", "close", "adj_close"]
SIGNAL_COLS = ["mom_short", "mom_long", "trend", "volume_ratio"]


@dataclass
class Panel:
    adj_close: pd.DataFrame   # index=date (str), columns=symbol
    close: pd.DataFrame
    volume: pd.DataFrame
    open: pd.DataFrame

    @classmethod
    def from_long(cls, bars: pd.DataFrame) -> "Panel":
        piv = lambda col: bars.pivot(index="date", columns="symbol", values=col).sort_index()  # noqa: E731
        return cls(adj_close=piv("adj_close"), close=piv("close"), volume=piv("volume"), open=piv("open"))


def compute_feature_panel(panel: Panel, cfg: FeatureCfg) -> Dict[str, pd.DataFrame]:
    """Full-history feature panels (date x symbol). Each cell at date t uses only data <= t."""
    ac, cl, vo = panel.adj_close, panel.close, panel.volume
    k = cfg.skip_last_day
    lag = ac.shift(k)                                   # price k days ago (skip most recent day)
    mom_s = lag / ac.shift(k + cfg.mom_short) - 1.0
    mom_l = lag / ac.shift(k + cfg.mom_long) - 1.0
    ma = ac.rolling(cfg.trend_ma, min_periods=cfg.trend_ma).mean()
    trend = ac / ma - 1.0
    logret = np.log(ac / ac.shift(1))
    vol = logret.rolling(cfg.vol_window, min_periods=cfg.vol_window).std() * np.sqrt(252.0)
    dv = (cl * vo).rolling(cfg.volume_ma, min_periods=cfg.volume_ma).mean()
    vol_ratio = vo / vo.rolling(cfg.volume_ma, min_periods=cfg.volume_ma).mean().shift(1)
    return {"mom_short": mom_s, "mom_long": mom_l, "trend": trend, "above_trend": (trend > 0).astype(float),
            "vol": vol, "dollar_volume": dv, "volume_ratio": vol_ratio, "close": cl, "adj_close": ac}


def features_at(fp: Dict[str, pd.DataFrame], as_of: str, symbols: Optional[List[str]] = None) -> pd.DataFrame:
    """Cross-section of raw features for the latest date <= as_of. Index = symbol."""
    dates = fp["close"].index
    valid = dates[dates <= as_of]
    if len(valid) == 0:
        return pd.DataFrame(columns=FEATURE_COLS)
    d = valid[-1]
    rows = {name: df.loc[d] for name, df in fp.items()}
    out = pd.DataFrame(rows)
    out.index.name = "symbol"
    out["date"] = d
    if symbols is not None:
        out = out.reindex([s for s in symbols if s in out.index])
    return out


def winsorize(s: pd.Series, pct: float) -> pd.Series:
    if s.dropna().empty or pct <= 0:
        return s
    lo, hi = s.quantile(pct), s.quantile(1 - pct)
    return s.clip(lower=lo, upper=hi)


def zscore(s: pd.Series) -> pd.Series:
    sd = s.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=s.index)
    return (s - s.mean()) / sd


def normalize_cross_section(feat: pd.DataFrame, cfg: FeatureCfg) -> pd.DataFrame:
    """Add z_<signal> columns: winsorized, standardized within the eligible universe."""
    out = feat.copy()
    for c in SIGNAL_COLS:
        if c in out:
            out[f"z_{c}"] = zscore(winsorize(out[c].astype(float), cfg.winsor_pct))
    return out


def eligibility(feat: pd.DataFrame, cfg: FeatureCfg) -> pd.Series:
    """Liquidity + completeness screen. True = tradable today."""
    need = ["mom_short", "mom_long", "trend", "vol", "dollar_volume"]
    complete = feat[need].notna().all(axis=1)
    liquid = feat["dollar_volume"] >= cfg.min_dollar_volume
    return complete & liquid


def market_regime(feat: pd.DataFrame, benchmark: str, cfg: RegimeCfg) -> Dict[str, object]:
    """SPY trend/vol state -> exposure multiplier."""
    if benchmark not in feat.index or pd.isna(feat.loc[benchmark, "trend"]) or pd.isna(feat.loc[benchmark, "vol"]):
        return {"state": "unknown", "multiplier": cfg.bear, "spy_trend": None, "spy_vol": None}
    t = float(feat.loc[benchmark, "trend"])
    v = float(feat.loc[benchmark, "vol"])
    if t > 0 and v < cfg.spy_vol_threshold:
        state, m = "bull", cfg.bull
    elif t <= 0 and v >= cfg.spy_vol_threshold:
        state, m = "bear", cfg.bear
    else:
        state, m = "neutral", cfg.neutral
    return {"state": state, "multiplier": m, "spy_trend": t, "spy_vol": v}
