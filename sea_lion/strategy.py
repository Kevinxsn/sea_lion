"""Strategy engine: quant + capped AI ensemble -> ranked candidates -> target weights.

The quant-only score is always kept separately so the AI contribution is observable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from .config import FeatureCfg, RiskCfg, StrategyCfg
from .features import eligibility, normalize_cross_section


@dataclass
class AIScore:
    score: float            # bounded impact in [-1,1]
    confidence: float
    risk_flags: List[str]
    evidence_ids: List[str]
    horizon_days: int = 5


def quant_scores(feat: pd.DataFrame, fcfg: FeatureCfg, scfg: StrategyCfg) -> pd.DataFrame:
    """Return feat with eligibility, z-scores, and quant_score in [-1, 1]."""
    out = feat.copy()
    out["eligible"] = eligibility(out, fcfg)
    elig = out[out["eligible"]]
    normed = normalize_cross_section(elig, fcfg)
    w = dict(scfg.signal_weights)
    tot = sum(w.values()) or 1.0
    w = {k: v / tot for k, v in w.items()}
    z = sum(w.get(c, 0.0) * normed[f"z_{c}"].fillna(0.0) for c in ["mom_short", "mom_long", "trend", "volume_ratio"])
    out["quant_score"] = np.nan
    out.loc[normed.index, "quant_score"] = np.tanh(z / 1.5)
    for c in ["mom_short", "mom_long", "trend", "volume_ratio"]:
        out[f"z_{c}"] = np.nan
        out.loc[normed.index, f"z_{c}"] = normed[f"z_{c}"]
    return out


def ensemble(scored: pd.DataFrame, ai: Dict[str, AIScore], scfg: StrategyCfg, rcfg: RiskCfg) -> pd.DataFrame:
    """Blend: ensemble = (1-cap)*quant + cap*ai. AI below confidence floor is neutral."""
    out = scored.copy()
    cap = min(scfg.ai_weight_cap, 0.20)
    out["ai_score"] = 0.0
    out["ai_confidence"] = 0.0
    out["ai_used"] = False
    for sym, a in ai.items():
        if sym not in out.index:
            continue
        if a.confidence >= rcfg.min_model_confidence:
            out.loc[sym, "ai_score"] = float(np.clip(a.score, -1.0, 1.0))
            out.loc[sym, "ai_used"] = True
        out.loc[sym, "ai_confidence"] = a.confidence
    out["ensemble_score"] = (1.0 - cap) * out["quant_score"].fillna(0.0) + cap * out["ai_score"]
    out["quant_only_score"] = out["quant_score"].fillna(0.0)
    return out


def select_candidates(scored: pd.DataFrame, scfg: StrategyCfg, score_col: str = "ensemble_score") -> List[str]:
    df = scored[scored["eligible"].astype(bool)]
    if scfg.require_above_trend:
        df = df[df["above_trend"] > 0]
    df = df[df[score_col] > scfg.min_candidate_score]
    df = df.sort_values(score_col, ascending=False)
    return list(df.index[: scfg.max_positions])


def target_weights(scored: pd.DataFrame, candidates: List[str], regime_multiplier: float,
                   scfg: StrategyCfg, rcfg: RiskCfg) -> Dict[str, float]:
    """Inverse-vol weights, gross = gross_max * regime, per-name cap with redistribution. Cash is the remainder."""
    if not candidates:
        return {}
    gross = rcfg.gross_exposure_max * float(regime_multiplier)
    if scfg.sizing == "inverse_vol":
        inv = {s: 1.0 / max(float(scored.loc[s, "vol"]), 0.05) for s in candidates}
    else:
        inv = {s: 1.0 for s in candidates}
    tot = sum(inv.values())
    w = {s: gross * v / tot for s, v in inv.items()}
    cap = rcfg.single_position_max
    for _ in range(10):   # redistribute excess above the cap to uncapped names
        over = {s: w[s] - cap for s in w if w[s] > cap + 1e-12}
        if not over:
            break
        excess = sum(over.values())
        for s in over:
            w[s] = cap
        free = [s for s in w if w[s] < cap - 1e-12]
        if not free:
            break
        share = sum(inv[s] for s in free)
        for s in free:
            w[s] = min(cap, w[s] + excess * inv[s] / share)
    return {s: round(x, 6) for s, x in w.items() if x > 0}


def build_proposal(scored: pd.DataFrame, regime: Dict, scfg: StrategyCfg, rcfg: RiskCfg
                   ) -> Tuple[Dict[str, float], Dict[str, float], List[str], List[str]]:
    """Returns (ai_assisted_weights, quant_only_weights, candidates, quant_only_candidates)."""
    cands = select_candidates(scored, scfg, "ensemble_score")
    qcands = select_candidates(scored, scfg, "quant_only_score")
    m = float(regime.get("multiplier", scfg.regime.bear))
    return (target_weights(scored, cands, m, scfg, rcfg), target_weights(scored, qcands, m, scfg, rcfg),
            cands, qcands)
