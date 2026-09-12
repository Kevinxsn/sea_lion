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
    high: Optional[pd.DataFrame] = None
    low: Optional[pd.DataFrame] = None

    @classmethod
    def from_long(cls, bars: pd.DataFrame) -> "Panel":
        piv = lambda col: bars.pivot(index="date", columns="symbol", values=col).sort_index()  # noqa: E731
        return cls(adj_close=piv("adj_close"), close=piv("close"), volume=piv("volume"), open=piv("open"),
                   high=piv("high") if "high" in bars else None, low=piv("low") if "low" in bars else None)


# ---------------------------------------------------------------- V2 feature registry (design §9)
# Every feature: family, definition, lookback, lag, missing behaviour, version. Production quant score
# still uses only the V1 signals; the rest are stored, shown to research passes, and used by risk.
FEATURE_REGISTRY: Dict[str, Dict[str, object]] = {
    "mom_short":     {"family": "momentum", "version": "v1", "lookback": 20, "lag": 1, "missing": "ineligible", "production": True,
                      "definition": "adj_close[t-1]/adj_close[t-21]-1"},
    "mom_long":      {"family": "momentum", "version": "v1", "lookback": 60, "lag": 1, "missing": "ineligible", "production": True,
                      "definition": "adj_close[t-1]/adj_close[t-61]-1"},
    "trend":         {"family": "trend", "version": "v1", "lookback": 50, "lag": 0, "missing": "ineligible", "production": True,
                      "definition": "adj_close/MA50-1"},
    "vol":           {"family": "volatility", "version": "v1", "lookback": 20, "lag": 0, "missing": "ineligible", "production": True,
                      "definition": "std(log returns,20)*sqrt(252)"},
    "dollar_volume": {"family": "liquidity", "version": "v1", "lookback": 20, "lag": 0, "missing": "ineligible", "production": True,
                      "definition": "mean(close*volume,20)"},
    "volume_ratio":  {"family": "liquidity", "version": "v1", "lookback": 20, "lag": 1, "missing": "zero", "production": True,
                      "definition": "volume/mean(volume,20 lagged 1)"},
    "mom_5":         {"family": "momentum", "version": "v2.0", "lookback": 5, "lag": 1, "missing": "nan", "production": False,
                      "definition": "adj_close[t-1]/adj_close[t-6]-1"},
    "mom_120":       {"family": "momentum", "version": "v2.0", "lookback": 120, "lag": 1, "missing": "nan", "production": False,
                      "definition": "adj_close[t-1]/adj_close[t-121]-1"},
    "trend_20":      {"family": "trend", "version": "v2.0", "lookback": 20, "lag": 0, "missing": "nan", "production": False,
                      "definition": "adj_close/MA20-1"},
    "trend_200":     {"family": "trend", "version": "v2.0", "lookback": 200, "lag": 0, "missing": "nan", "production": False,
                      "definition": "adj_close/MA200-1"},
    "ma50_slope":    {"family": "trend", "version": "v2.0", "lookback": 70, "lag": 0, "missing": "nan", "production": False,
                      "definition": "MA50[t]/MA50[t-20]-1"},
    "resid_1":       {"family": "mean_reversion", "version": "v2.0", "lookback": 1, "lag": 0, "missing": "nan", "production": False,
                      "definition": "1d return minus SPY 1d return"},
    "resid_5":       {"family": "mean_reversion", "version": "v2.0", "lookback": 5, "lag": 0, "missing": "nan", "production": False,
                      "definition": "5d return minus SPY 5d return"},
    "rs_spy_20":     {"family": "relative_strength", "version": "v2.0", "lookback": 20, "lag": 1, "missing": "nan", "production": False,
                      "definition": "mom_short minus SPY mom_short"},
    "rs_sector_20":  {"family": "relative_strength", "version": "v2.0", "lookback": 20, "lag": 1, "missing": "nan", "production": False,
                      "definition": "mom_short minus sector-ETF mom_short"},
    "downside_vol":  {"family": "volatility", "version": "v2.0", "lookback": 20, "lag": 0, "missing": "nan", "production": False,
                      "definition": "std of negative log returns,20 * sqrt(252)"},
    "range_20":      {"family": "volatility", "version": "v2.0", "lookback": 20, "lag": 0, "missing": "nan", "production": False,
                      "definition": "mean((high-low)/close,20)"},
    "gap_risk":      {"family": "volatility", "version": "v2.0", "lookback": 20, "lag": 0, "missing": "nan", "production": False,
                      "definition": "mean(|open/prev_close-1|,20)"},
    "beta_60":       {"family": "market_risk", "version": "v2.0", "lookback": 60, "lag": 0, "missing": "one", "production": False,
                      "definition": "cov(r,r_spy,60)/var(r_spy,60)"},
    "corr_spy_60":   {"family": "market_risk", "version": "v2.0", "lookback": 60, "lag": 0, "missing": "nan", "production": False,
                      "definition": "corr(r,r_spy,60)"},
    "drawdown_60":   {"family": "market_risk", "version": "v2.0", "lookback": 60, "lag": 0, "missing": "nan", "production": False,
                      "definition": "adj_close/max(adj_close,60)-1"},
}
V2_FEATURE_COLS = [k for k, v in FEATURE_REGISTRY.items() if v["version"] != "v1"]


def compute_feature_panel(panel: Panel, cfg: FeatureCfg, benchmark: str = "SPY",
                          sector_etf: Optional[Dict[str, str]] = None) -> Dict[str, pd.DataFrame]:
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
    out = {"mom_short": mom_s, "mom_long": mom_l, "trend": trend, "above_trend": (trend > 0).astype(float),
           "vol": vol, "dollar_volume": dv, "volume_ratio": vol_ratio, "close": cl, "adj_close": ac}
    # ---- V2 families (all backward-looking) ----
    out["mom_5"] = lag / ac.shift(k + 5) - 1.0
    out["mom_120"] = lag / ac.shift(k + 120) - 1.0
    out["trend_20"] = ac / ac.rolling(20, min_periods=20).mean() - 1.0
    out["trend_200"] = ac / ac.rolling(200, min_periods=200).mean() - 1.0
    out["ma50_slope"] = ma / ma.shift(20) - 1.0
    neg = logret.where(logret < 0, 0.0)
    out["downside_vol"] = neg.rolling(cfg.vol_window, min_periods=cfg.vol_window).std() * np.sqrt(252.0)
    if panel.high is not None and panel.low is not None:
        out["range_20"] = ((panel.high - panel.low) / cl).rolling(20, min_periods=20).mean()
    out["gap_risk"] = (panel.open / cl.shift(1) - 1.0).abs().rolling(20, min_periods=20).mean()
    out["drawdown_60"] = ac / ac.rolling(60, min_periods=20).max() - 1.0
    if benchmark in ac.columns:
        r = ac.pct_change()
        rb = r[benchmark]
        out["resid_1"] = r.sub(rb, axis=0)
        r5 = ac / ac.shift(5) - 1.0
        out["resid_5"] = r5.sub(r5[benchmark], axis=0)
        out["rs_spy_20"] = mom_s.sub(mom_s[benchmark], axis=0)
        cov = r.rolling(60, min_periods=40).cov(rb)
        var = rb.rolling(60, min_periods=40).var()
        out["beta_60"] = cov.div(var, axis=0)
        out["corr_spy_60"] = r.rolling(60, min_periods=40).corr(rb)
        if sector_etf:
            rs = mom_s.copy()
            for sym in rs.columns:
                etf = sector_etf.get(sym, benchmark)
                rs[sym] = mom_s[sym] - (mom_s[etf] if etf in mom_s.columns else mom_s[benchmark])
            out["rs_sector_20"] = rs
    return out


def snapshot_rows(feat: pd.DataFrame, cols: Optional[List[str]] = None) -> List[Dict[str, object]]:
    """Rows for feature_snapshots with explicit missingness flags."""
    cols = cols or [c for c in FEATURE_REGISTRY if c in feat.columns]
    rows = []
    for sym, r in feat.iterrows():
        vals, missing = {}, []
        for c in cols:
            v = r.get(c)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                missing.append(c)
            else:
                vals[c] = round(float(v), 6)
        rows.append({"symbol": sym, "values": vals, "missing": missing})
    return rows


def fundamental_features(store, symbols: List[str], as_of: str) -> Dict[str, Dict[str, Optional[float]]]:
    """Point-in-time (filed <= as_of) YoY growth, margin, and filing age. ETFs/no data -> None + missing flag."""
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for sym in symbols:
        f: Dict[str, Optional[float]] = {"rev_yoy": None, "eps_yoy": None, "net_margin": None, "days_since_filing": None}
        rev = [r for r in store.fundamentals_asof(sym, "revenue", as_of) if not str(r.get("fp", "")).endswith("_FY")]
        ni = [r for r in store.fundamentals_asof(sym, "net_income", as_of) if not str(r.get("fp", "")).endswith("_FY")]
        eps = [r for r in store.fundamentals_asof(sym, "eps_diluted", as_of) if not str(r.get("fp", "")).endswith("_FY")]

        def yoy(rows):
            if len(rows) < 5:
                return None
            last, prev = rows[-1], rows[-5]
            return (last["value"] / prev["value"] - 1.0) if prev["value"] else None
        f["rev_yoy"] = yoy(rev)
        f["eps_yoy"] = yoy(eps) if eps and eps[-1]["value"] and len(eps) >= 5 and eps[-5]["value"] > 0 else None
        if rev and ni and rev[-1]["period_end"] == ni[-1]["period_end"] and rev[-1]["value"]:
            f["net_margin"] = ni[-1]["value"] / rev[-1]["value"]
        latest_filed = max([r["filed"] for r in rev + ni] or [None])
        if latest_filed:
            from datetime import date as _d
            f["days_since_filing"] = float((_d.fromisoformat(as_of) - _d.fromisoformat(latest_filed)).days)
        out[sym] = f
    return out


def macro_features(store, as_of: str, cutoff: Optional[str] = None) -> Dict[str, Optional[float]]:
    """Market-level macro state: observations with effective_date <= as_of that were AVAILABLE by the
    run cutoff (live retrieval happens after the session, so availability is the run time)."""
    def series(name):
        rows = store.macro_series(name, cutoff or "9999")
        return [(r["effective_date"], r["value"]) for r in rows if r["effective_date"] <= as_of]

    def level(name):
        s = series(name)
        return s[-1][1] if s else None

    def change(name, n):
        s = series(name)
        return (s[-1][1] - s[-1 - n][1]) if len(s) > n else None
    return {"dgs10": level("DGS10"), "dgs10_chg20": change("DGS10", 20), "t10y2y": level("T10Y2Y"), "vix": level("VIXCLS"),
            "vix_chg5": change("VIXCLS", 5), "hy_spread": level("BAMLH0A0HYM2"), "hy_spread_chg20": change("BAMLH0A0HYM2", 20),
            "dollar_chg20": change("DTWEXBGS", 20)}


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
