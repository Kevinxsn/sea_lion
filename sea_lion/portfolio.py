"""V2 portfolio rules (design §12): rank hysteresis, correlation clusters, portfolio beta,
scenario exposure checks, and binary-event concentration. Deterministic; proposal-side only."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd

from .config import HysteresisCfg, PortfolioRiskCfg


def select_with_hysteresis(scored: pd.DataFrame, holdings: Iterable[str], cfg: HysteresisCfg, max_positions: int,
                           score_col: str = "ensemble_score", require_above_trend: bool = True,
                           forced_exits: Optional[Set[str]] = None) -> Tuple[List[str], Dict[str, str]]:
    """Entry at top-N, retention while in top-retention_rank or within a score band of the Nth,
    replacement only when the incoming candidate beats the outgoing by margin + round-trip cost."""
    why: Dict[str, str] = {}
    df = scored[scored["eligible"].astype(bool)].copy()
    if require_above_trend:
        df = df[df["above_trend"] > 0]
    df = df[df[score_col] > 0].sort_values(score_col, ascending=False)
    ranked = list(df.index)
    rank = {s: i + 1 for i, s in enumerate(ranked)}
    score = df[score_col].to_dict()
    nth_score = score[ranked[cfg.entry_rank - 1]] if len(ranked) >= cfg.entry_rank else (score[ranked[-1]] if ranked else 0.0)
    holdings = set(holdings)
    forced = set(forced_exits or [])
    retained: List[str] = []
    for h in sorted(holdings, key=lambda s: rank.get(s, 10**6)):
        if h in forced or h not in rank:
            why[h] = "forced_exit" if h in forced else "no_longer_eligible"
            continue
        if rank[h] <= cfg.retention_rank or score[h] >= nth_score - cfg.score_band:
            retained.append(h)
            why[h] = f"retained_rank_{rank[h]}"
        else:
            why[h] = f"dropped_rank_{rank[h]}"
    selected = list(retained)
    entrants = [s for s in ranked[: cfg.entry_rank] if s not in holdings]
    for s in entrants:
        if len(selected) < max_positions:
            selected.append(s)
            why[s] = f"entered_rank_{rank[s]}"
            continue
        # portfolio is full: replace the weakest retained holding only if the margin clears costs
        weakest = min(retained, key=lambda x: score[x]) if retained else None
        if weakest and score[s] - score[weakest] >= cfg.replacement_margin + cfg.round_trip_cost:
            selected.remove(weakest); retained.remove(weakest)
            why[weakest] = f"replaced_by_{s}"
            selected.append(s); retained.append(s)
            why[s] = f"replaced_{weakest}"
        else:
            why[s] = "blocked_by_hysteresis"
    return selected[:max_positions], why


def correlation_clusters(adj_close: pd.DataFrame, as_of: str, window: int, threshold: float) -> Dict[str, int]:
    """Union-find clusters of names whose trailing-return correlation exceeds threshold."""
    px = adj_close[adj_close.index <= as_of].tail(window + 1)
    r = px.pct_change().dropna(how="all")
    if len(r) < max(20, window // 3):
        return {s: i for i, s in enumerate(adj_close.columns)}
    c = r.corr()
    syms = list(c.columns)
    parent = {s: s for s in syms}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            v = c.at[a, b]
            if pd.notna(v) and v >= threshold:
                parent[find(a)] = find(b)
    roots = {}
    return {s: roots.setdefault(find(s), len(roots)) for s in syms}


def cluster_exposure(weights: Dict[str, float], clusters: Dict[str, int]) -> Dict[int, float]:
    out: Dict[int, float] = {}
    for s, w in weights.items():
        cid = clusters.get(s, -1)
        out[cid] = out.get(cid, 0.0) + w
    return out


def portfolio_beta(weights: Dict[str, float], betas: Dict[str, float]) -> float:
    return float(sum(w * float(betas.get(s, 1.0) if betas.get(s) is not None else 1.0) for s, w in weights.items()))


def scenario_checks(weights: Dict[str, float], betas: Dict[str, float], sectors: Dict[str, str], clusters: Dict[str, int],
                    gap_risk: Dict[str, float], cfg: PortfolioRiskCfg) -> Dict[str, float]:
    """Deterministic exposure estimates as a fraction of equity (negative = loss)."""
    out: Dict[str, float] = {}
    pb = portfolio_beta(weights, betas)
    out["market_-3pct"] = round(cfg.scenarios.get("market_-3pct", -0.03) * pb, 4)
    tech = sum(w for s, w in weights.items() if sectors.get(s) in ("Technology", "Communication Services"))
    out["tech_-5pct"] = round(cfg.scenarios.get("tech_-5pct", -0.05) * tech, 4)
    rate_sens = sum(w for s, w in weights.items() if sectors.get(s) in ("Real Estate", "Utilities", "Bonds"))
    out["rates_shock_-3pct_duration"] = round(-0.03 * rate_sens, 4)
    if weights:
        out["largest_gap_-10pct"] = round(cfg.scenarios.get("largest_gap_-10pct", -0.10) * max(weights.values()), 4)
        ce = cluster_exposure(weights, clusters)
        out["largest_cluster_-5pct"] = round(-0.05 * max(ce.values()), 4)
        out["overnight_adverse"] = round(-sum(w * 2.0 * float(gap_risk.get(s) or 0.01) for s, w in weights.items()), 4)
    return out


def binary_event_symbols(events: List[Dict[str, object]], within_days: int = 3, as_of: str = "") -> Set[str]:
    """Symbols with a high-impact binary catalyst (earnings/guidance/regulatory decision) in the window."""
    out: Set[str] = set()
    for e in events:
        if e.get("event_type") in ("earnings", "guidance", "regulatory", "non_reliance_restatement", "acquisition_disposition") \
                and float(e.get("importance") or 0) >= 0.6:
            out.add(str(e["primary_symbol"]))
    return out
