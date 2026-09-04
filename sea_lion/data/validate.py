"""Data validation: reject stale, missing, duplicated, or structurally invalid observations.

Returns a report; symbols that fail are excluded from trading. A failing benchmark
aborts the run (the regime filter cannot be computed)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List

import pandas as pd


@dataclass
class ValidationReport:
    as_of: str
    ok_symbols: List[str] = field(default_factory=list)
    rejected: Dict[str, str] = field(default_factory=dict)
    latest_date: str = ""
    stale: bool = False
    benchmark_ok: bool = True

    @property
    def ok(self) -> bool:
        return self.benchmark_ok and not self.stale and len(self.ok_symbols) > 0

    def to_dict(self) -> dict:
        return {"as_of": self.as_of, "ok": self.ok, "ok_symbols": self.ok_symbols, "rejected": self.rejected,
                "latest_date": self.latest_date, "stale": self.stale, "benchmark_ok": self.benchmark_ok}


def validate_bars(df: pd.DataFrame, symbols: List[str], benchmark: str, as_of: date,
                  min_rows: int, max_staleness_days: int) -> ValidationReport:
    rep = ValidationReport(as_of=as_of.isoformat())
    if df.empty:
        rep.stale = True
        rep.benchmark_ok = False
        rep.rejected = {s: "no_data" for s in symbols}
        return rep
    df = df[df["date"] <= as_of.isoformat()]
    if df.empty:
        rep.stale = True
        rep.benchmark_ok = False
        rep.rejected = {s: "no_data_before_as_of" for s in symbols}
        return rep
    latest_overall = max(df["date"])
    rep.latest_date = latest_overall
    if date.fromisoformat(latest_overall) < as_of - timedelta(days=max_staleness_days):
        rep.stale = True
    for sym in symbols:
        sub = df[df["symbol"] == sym]
        reason = _check_symbol(sub, latest_overall, min_rows)
        if reason:
            rep.rejected[sym] = reason
        else:
            rep.ok_symbols.append(sym)
    rep.benchmark_ok = benchmark in rep.ok_symbols
    return rep


def _check_symbol(sub: pd.DataFrame, latest_overall: str, min_rows: int) -> str:
    if sub.empty:
        return "no_data"
    if sub["date"].duplicated().any():
        return "duplicate_dates"
    if len(sub) < min_rows:
        return f"insufficient_history:{len(sub)}<{min_rows}"
    if sub[["open", "high", "low", "close", "adj_close"]].isna().any().any():
        return "nan_prices"
    if (sub[["open", "high", "low", "close", "adj_close"]] <= 0).any().any():
        return "nonpositive_price"
    if (sub["high"] < sub["low"]).any():
        return "high_below_low"
    if ((sub["close"] > sub["high"] * 1.001) | (sub["close"] < sub["low"] * 0.999)).any():
        return "close_outside_range"
    if sub["volume"].isna().any() or (sub["volume"] < 0).any():
        return "bad_volume"
    if max(sub["date"]) != latest_overall:
        return f"lagging_latest_bar:{max(sub['date'])}"
    # absurd jumps usually mean a bad split adjustment
    r = sub["adj_close"].pct_change().abs()
    if (r > 0.6).any():
        return "suspicious_jump"
    return ""
