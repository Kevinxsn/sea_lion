"""Forecast records, outcome scoring, calibration, and the AI overlay (design §10, §11).

- Forecasts are frozen (append-only) at decision time with the base prices needed to score them.
- Outcomes are scored at 5/10/20 sessions from close-to-close adjusted returns versus SPY.
- Calibration is empirical: shrink toward 0.5 until >= min_samples scored outcomes exist, then a
  1-D logistic map fitted per horizon (Brier/reliability reported either way).
- The overlay in [-1, 1] is the calibrated, quality-weighted, shrunk sign probability."""
from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .store import Store

HKEYS = {5: "5d", 10: "10d", 20: "20d"}


def forecast_id(run_id: str, arm: str, symbol: str) -> str:
    return "fc_" + hashlib.sha1(f"{run_id}|{arm}|{symbol}".encode()).hexdigest()[:16]


def freeze(store: Store, run_id: str, arm: str, as_of: str, decision_time: str, symbol: str, horizons: Dict[str, Dict[str, float]],
           raw: Dict[str, Any], calibrated: Dict[str, Any], evidence_quality: float, disagreement: float, abstain: bool,
           evidence_ids: List[str], risk_flags: List[str], overlay: float, base_price: Optional[float],
           bench_base: Optional[float], version: str) -> str:
    fid = forecast_id(run_id, arm, symbol)
    store.save_forecast({"forecast_id": fid, "run_id": run_id, "arm": arm, "symbol": symbol, "as_of": as_of,
                         "decision_time": decision_time, "forecast_version": version, "horizons": horizons, "raw": raw,
                         "calibrated": calibrated, "evidence_quality": evidence_quality, "model_disagreement": disagreement,
                         "abstain": abstain, "evidence_ids": evidence_ids, "risk_flags": risk_flags, "overlay_score": overlay,
                         "base_price": base_price, "benchmark_base": bench_base})
    return fid


# ---------------------------------------------------------------- calibration
def shrink(p: float, k: float) -> float:
    return 0.5 + (p - 0.5) * k


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def apply_calibration(p: float, params: Optional[Dict[str, float]]) -> float:
    if not params:
        return p
    z = params["a"] + params["b"] * logit(p)
    return 1.0 / (1.0 + math.exp(-z))


def fit_logistic(ps: List[float], ys: List[int], iters: int = 50) -> Dict[str, float]:
    """1-D logistic recalibration y ~ sigmoid(a + b*logit(p)) via Newton-Raphson with light ridge."""
    x = np.array([logit(p) for p in ps]); y = np.array(ys, dtype=float)
    a, b, lam = 0.0, 1.0, 1e-2
    for _ in range(iters):
        z = a + b * x
        q = 1 / (1 + np.exp(-z))
        w = q * (1 - q) + 1e-9
        g = np.array([np.sum(q - y), np.sum((q - y) * x) + lam * (b - 1)])
        H = np.array([[np.sum(w), np.sum(w * x)], [np.sum(w * x), np.sum(w * x * x) + lam]])
        step = np.linalg.solve(H, g)
        a, b = a - step[0], b - step[1]
        if np.abs(step).max() < 1e-8:
            break
    return {"a": float(a), "b": float(b)}


def calibration_stats(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not outcomes:
        return {"n": 0}
    ps = np.array([o["p_positive"] for o in outcomes], dtype=float)
    hs = np.array([o["hit"] for o in outcomes], dtype=float)
    brier = float(np.mean((ps - hs) ** 2))
    acc = float(np.mean((ps > 0.5) == (hs > 0.5)))
    bins = [(0, .4), (.4, .5), (.5, .6), (.6, .7), (.7, 1.01)]
    rel = []
    for lo, hi in bins:
        m = (ps >= lo) & (ps < hi)
        if m.sum():
            rel.append({"bin": f"[{lo:.1f},{hi:.1f})", "n": int(m.sum()), "mean_p": round(float(ps[m].mean()), 3),
                        "hit_rate": round(float(hs[m].mean()), 3)})
    excess = np.array([o["excess_return"] for o in outcomes], dtype=float)
    ic = float(np.corrcoef(ps, excess)[0, 1]) if len(ps) > 3 and ps.std() > 0 and excess.std() > 0 else None
    return {"n": int(len(ps)), "brier": round(brier, 4), "directional_accuracy": round(acc, 3), "reliability": rel,
            "rank_ic": None if ic is None or math.isnan(ic) else round(ic, 3),
            "mean_excess_when_p_gt_0.55": round(float(excess[ps > 0.55].mean()), 5) if (ps > 0.55).any() else None}


def maybe_fit(store: Store, horizon: int, min_samples: int, version: str) -> Optional[Dict[str, Any]]:
    outs = [o for o in store.scored_outcomes(horizon) if o.get("p_positive") is not None and o.get("hit") is not None]
    if len(outs) < min_samples:
        return None
    params = fit_logistic([o["p_positive"] for o in outs], [int(o["hit"]) for o in outs])
    stats = calibration_stats(outs)
    store.save_calibration(horizon, version, len(outs), params, stats)
    return {"params": params, "stats": stats}


def calibrate_forecast(store: Store, horizons: Dict[str, Dict[str, float]], shrink_k: float) -> Tuple[Dict[str, Dict[str, float]], str]:
    """Return calibrated horizons and a factor label. Shrink toward 0.5 when no fitted model exists."""
    out: Dict[str, Dict[str, float]] = {}
    mode = "shrunk"
    for h, key in HKEYS.items():
        raw = horizons.get(key, {})
        p = float(raw.get("p_positive_excess_return", 0.5))
        cal = store.active_calibration(h)
        if cal:
            import json as _j
            p2 = apply_calibration(p, _j.loads(cal["params_json"]))
            mode = "fitted"
        else:
            p2 = shrink(p, shrink_k)
        out[key] = {"p_positive_excess_return": round(p2, 4),
                    "expected_excess_return_bps": float(raw.get("expected_excess_return_bps", 0)) * (shrink_k if not cal else 1.0)}
    return out, mode


def overlay_score(calibrated: Dict[str, Dict[str, float]], evidence_quality: float, freshness: float, abstain: bool,
                  quality_floor: float, cap: float = 1.0) -> Tuple[float, Dict[str, float]]:
    """event_score = mean over horizons of 2*(p-0.5); quality_multiplier = quality * freshness. Bounded."""
    if abstain or evidence_quality < quality_floor:
        return 0.0, {"event_score": 0.0, "quality_multiplier": 0.0, "reason": "abstain" if abstain else "evidence_quality_below_floor"}
    es = float(np.mean([2 * (v["p_positive_excess_return"] - 0.5) for v in calibrated.values()]))
    qm = float(max(0.0, min(1.0, evidence_quality)) * max(0.0, min(1.0, freshness)))
    return float(max(-cap, min(cap, es * qm))), {"event_score": round(es, 4), "quality_multiplier": round(qm, 4), "reason": "ok"}


# ---------------------------------------------------------------- outcomes
def score_outcomes(store: Store, sessions: List[str], adj_close: "Any", benchmark: str, horizons: List[int],
                   stale_days: int = 90) -> Dict[str, Any]:
    """adj_close: DataFrame(index=date str, columns=symbol). Scores every forecast whose horizon has matured."""
    out = {"scored": 0, "pending": 0, "exceptions": 0}
    sess_index = {d: i for i, d in enumerate(sessions)}
    latest = adj_close.index.max() if len(adj_close.index) else None
    for h in horizons:
        for f in store.unscored_forecasts(h):
            as_of = f["as_of"]
            if as_of not in sess_index:
                # decision date not a session in our calendar window: exception if old
                if latest and (np.datetime64(latest) - np.datetime64(as_of)).astype(int) > stale_days:
                    store.save_outcome({"forecast_id": f["forecast_id"], "horizon": h, "status": "exception_no_session"})
                    out["exceptions"] += 1
                else:
                    out["pending"] += 1
                continue
            i = sess_index[as_of] + h
            if i >= len(sessions):
                out["pending"] += 1
                continue
            matured = sessions[i]
            sym = f["symbol"]
            if matured not in adj_close.index or sym not in adj_close.columns or as_of not in adj_close.index:
                out["pending"] += 1
                continue
            p0, p1 = adj_close.at[as_of, sym], adj_close.at[matured, sym]
            b0, b1 = adj_close.at[as_of, benchmark], adj_close.at[matured, benchmark]
            if any(map(lambda v: v is None or (isinstance(v, float) and math.isnan(v)), (p0, p1, b0, b1))):
                out["pending"] += 1
                continue
            r, b = float(p1 / p0 - 1), float(b1 / b0 - 1)
            import json as _j
            hz = _j.loads(f["calibrated_json"] or "{}") or _j.loads(f["horizons_json"] or "{}")
            p = float((hz.get(HKEYS[h]) or {}).get("p_positive_excess_return", 0.5))
            hit = int(r - b > 0)
            store.save_outcome({"forecast_id": f["forecast_id"], "horizon": h, "matured_date": matured, "realized_return": r,
                                "benchmark_return": b, "excess_return": r - b, "p_positive": p, "hit": hit, "brier": (p - hit) ** 2,
                                "status": "scored"})
            out["scored"] += 1
    return out
