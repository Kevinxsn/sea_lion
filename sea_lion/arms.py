"""Champion/challenger arms (design §15.3, §11): from ONE cutoff and ONE quant score, build
  A: quant-only, B: V1 headline overlay, C: V2 verified-event overlay,
run the deterministic risk engine on each, simulate fills for the non-orders arms with the local
simulator, and record rank / membership / weight / order divergence versus the quant baseline."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from . import risk as RK, strategy as S
from .broker.sim import SimBroker, empty_state
from .config import Settings
from .execution import limit_price
from .portfolio import select_with_hysteresis
from .store import Store

ARMS = ("A", "B", "C")
ARM_NAMES = {"A": "quant_only", "B": "v1_headline_overlay", "C": "v2_verified_event_overlay"}


def build_arm(arm: str, scored_base: pd.DataFrame, ai_v1: Dict[str, S.AIScore], ai_v2: Dict[str, S.AIScore], regime: Dict[str, Any],
              cfg: Settings, holdings: List[str]) -> Dict[str, Any]:
    """Proposal for one arm. Hysteresis (if enabled) applies to the orders arm's selection only when
    configured; the shadow variant is always recorded for comparison."""
    ai = {"A": {}, "B": ai_v1, "C": ai_v2}[arm]
    scored = S.ensemble(scored_base, ai, cfg.strategy, cfg.risk)
    cands = S.select_candidates(scored, cfg.strategy, "ensemble_score")
    m = float(regime.get("multiplier", cfg.strategy.regime.bear))
    weights = S.target_weights(scored, cands, m, cfg.strategy, cfg.risk)
    hyst_cands, hyst_why = select_with_hysteresis(scored, holdings, cfg.v2.hysteresis, cfg.strategy.max_positions,
                                                  require_above_trend=cfg.strategy.require_above_trend)
    hyst_weights = S.target_weights(scored, hyst_cands, m, cfg.strategy, cfg.risk)
    use_hyst = cfg.v2.hysteresis.enabled
    ranks = {s: i + 1 for i, s in enumerate(scored[scored["eligible"].astype(bool)].sort_values("ensemble_score", ascending=False).index)}
    return {"arm": arm, "name": ARM_NAMES[arm], "candidates": hyst_cands if use_hyst else cands,
            "target_weights": hyst_weights if use_hyst else weights, "plain_candidates": cands, "plain_weights": weights,
            "hysteresis_candidates": hyst_cands, "hysteresis_weights": hyst_weights, "hysteresis_why": hyst_why,
            "hysteresis_applied": use_hyst, "ranks": ranks,
            "scores": {s: round(float(v), 6) for s, v in scored["ensemble_score"].items()},
            "ai_contribution": {s: round(float(scored.loc[s, "ai_score"] * cfg.strategy.ai_weight_cap), 6) for s in scored.index if scored.loc[s, "ai_score"] != 0}}


def divergence(base: Dict[str, Any], other: Dict[str, Any], base_intents: List[RK.OrderIntent],
               other_intents: List[RK.OrderIntent]) -> Dict[str, Any]:
    bw, ow = base["target_weights"], other["target_weights"]
    syms = set(bw) | set(ow)
    rank_changes = {s: (base["ranks"].get(s), other["ranks"].get(s)) for s in set(base["ranks"]) | set(other["ranks"])
                    if base["ranks"].get(s) != other["ranks"].get(s)}
    bi = {(i.symbol, i.side): i.notional for i in base_intents}
    oi = {(i.symbol, i.side): i.notional for i in other_intents}
    caused = [k for k in oi if k not in bi]
    prevented = [k for k in bi if k not in oi]
    resized = [k for k in oi if k in bi and abs(oi[k] - bi[k]) > 1.0]
    return {"rank_changes": {s: list(v) for s, v in rank_changes.items()}, "n_rank_changes": len(rank_changes),
            "membership_added": sorted(set(ow) - set(bw)), "membership_removed": sorted(set(bw) - set(ow)),
            "weight_distance": round(sum(abs(bw.get(s, 0.0) - ow.get(s, 0.0)) for s in syms), 6),
            "orders_caused": len(caused), "orders_prevented": len(prevented), "orders_resized": len(resized),
            "notional_delta": round(sum(oi.values()) - sum(bi.values()), 2),
            "caused": [f"{s}:{d}" for s, d in caused], "prevented": [f"{s}:{d}" for s, d in prevented]}


class ShadowArm:
    """Simulated portfolio for a non-orders arm: settles yesterday's intents at today's open, marks
    to close, and applies the same risk engine to that arm's own book."""

    def __init__(self, store: Store, mode: str, arm: str, cfg: Settings):
        self.store, self.mode, self.arm, self.cfg = store, mode, arm, cfg
        key = f"arm_state:{mode}:{arm}"
        state = store.kv_get(key) or empty_state(cfg.broker.initial_cash)
        self.broker = SimBroker(state, cfg.v2.arms.shadow_slippage_bps, persist=lambda st: store.kv_set(key, st))

    def settle(self, as_of: str, opens: Dict[str, float], closes: Dict[str, float]) -> None:
        if self.broker.s.get("date") != as_of:
            self.broker.settle(as_of, opens, closes)
        else:
            self.broker.mark(closes, as_of)

    def account_state(self, prices: Dict[str, float], hwm_key: str, sod: Optional[float] = None, **extra) -> RK.AccountState:
        a = self.broker.account()
        hwm = max(float(self.store.kv_get(hwm_key, 0.0) or 0.0), a.equity)
        self.store.kv_set(hwm_key, hwm)
        return RK.AccountState(equity=a.equity, cash=a.cash, positions={s: p.qty for s, p in a.positions.items()}, prices=prices,
                               start_of_day_equity=sod if sod is not None else (a.last_equity or a.equity), high_water_mark=hwm, **extra)

    def submit(self, run_id: str, intents: List[RK.OrderIntent], prices: Dict[str, float]) -> List[Dict[str, Any]]:
        out = []
        for it in intents:
            px = prices.get(it.symbol) or it.reference_price
            lim = limit_price(px, it.side, self.cfg.broker.limit_offset_bps)
            pos = self.broker.s["positions"].get(it.symbol, {}).get("qty", 0.0)
            qty = pos if (it.side == "sell" and it.close_position) else min(it.notional / lim, pos) if it.side == "sell" else it.notional / lim
            if qty <= 0:
                continue
            self.broker.submit_limit_order(it.symbol, it.side, qty, lim, f"{self.arm}-{run_id}-{it.symbol}-{it.side}", reference_price=px)
            out.append({"symbol": it.symbol, "side": it.side, "qty": round(qty, 4), "limit": lim, "notional": round(it.notional, 2)})
        return out

    def snapshot(self) -> Dict[str, Any]:
        a = self.broker.account()
        return {"sim_equity": round(a.equity, 2), "sim_cash": round(a.cash, 2),
                "sim_positions": {s: round(p.qty, 4) for s, p in a.positions.items()}}
