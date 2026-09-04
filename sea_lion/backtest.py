"""Walk-forward, quant-only backtest using the SAME feature, strategy, risk, and fill code
as the daily pipeline. No look-ahead: features at date t use bars <= t, orders decided after
the close of t fill at the open of t+1 with slippage (expiring if the open gaps > 5%).

The AI layer is deliberately excluded (design §11): historical news cannot be reconstructed
without look-ahead, so the AI contribution is evaluated prospectively in shadow/paper.
Known limitation: the universe is today's list, so results carry survivorship bias.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from . import features as F, risk as RK, strategy as S
from .broker.sim import SimBroker, empty_state
from .config import Settings
from .execution import limit_price
from .report import perf_metrics

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    equity: pd.Series
    benchmark: pd.Series
    trades: List[Dict[str, Any]]
    metrics: Dict[str, Any]
    exposure: pd.Series
    regime_counts: Dict[str, int] = field(default_factory=dict)
    n_rebalances: int = 0

    def summary(self) -> Dict[str, Any]:
        return {**self.metrics, "n_trades": len(self.trades), "n_rebalances": self.n_rebalances,
                "avg_exposure": float(self.exposure.mean()) if len(self.exposure) else None,
                "regime_counts": self.regime_counts, "start": str(self.equity.index[0]) if len(self.equity) else None,
                "end": str(self.equity.index[-1]) if len(self.equity) else None}


def run_backtest(cfg: Settings, bars: pd.DataFrame, start: str, end: str, initial_cash: Optional[float] = None,
                 rebalance_days: int = 1, cost_bps: Optional[float] = None) -> BacktestResult:
    cash0 = initial_cash or cfg.broker.initial_cash
    slip = cfg.broker.sim_slippage_bps if cost_bps is None else cost_bps
    symbols = sorted(bars["symbol"].unique())
    panel = F.Panel.from_long(bars)
    fp = F.compute_feature_panel(panel, cfg.features)
    dates = [d for d in panel.close.index if start <= d <= end]
    broker = SimBroker(empty_state(cash0), slippage_bps=slip, commission_usd=cfg.broker.sim_commission_usd)
    eq, bench, expo, trades = [], [], [], []
    regime_counts: Dict[str, int] = {}
    hwm = cash0
    n_reb = 0
    sectors = cfg.universe.symbols
    bench_sym = cfg.universe.benchmark
    for i, d in enumerate(dates):
        opens = {s: float(v) for s, v in panel.open.loc[d].dropna().items()}
        closes = {s: float(v) for s, v in panel.close.loc[d].dropna().items()}
        sod_equity = broker.equity()
        fills = broker.settle(d, opens, closes)
        for f in fills:
            trades.append({"date": d, "symbol": f.symbol, "side": f.side, "qty": f.filled_qty, "price": f.filled_avg_price})
        equity = broker.equity()
        hwm = max(hwm, equity)
        eq.append(equity)
        bench.append(closes.get(bench_sym, float("nan")))
        acct = broker.account()
        gross = sum(p.market_value for p in acct.positions.values())
        expo.append(gross / equity if equity > 0 else 0.0)
        if i % rebalance_days != 0:
            continue
        feat = F.features_at(fp, d, symbols)
        if feat.empty:
            continue
        scored = S.quant_scores(feat, cfg.features, cfg.strategy)
        regime = F.market_regime(feat, bench_sym, cfg.strategy.regime)
        regime_counts[regime["state"]] = regime_counts.get(regime["state"], 0) + 1
        scored = S.ensemble(scored, {}, cfg.strategy, cfg.risk)
        weights, _, cands, _ = S.build_proposal(scored, regime, cfg.strategy, cfg.risk)
        state = RK.AccountState(equity=equity, cash=acct.cash, positions={s: p.qty for s, p in acct.positions.items()},
                                prices=closes, start_of_day_equity=sod_equity, high_water_mark=hwm)
        res = RK.evaluate(weights, state, sectors, cfg.risk, rank_order=cands)
        n_reb += 1
        for it in res.intents:
            lim = limit_price(it.reference_price, it.side, cfg.broker.limit_offset_bps)
            qty = it.notional / lim
            if it.side == "sell":
                qty = min(qty, state.positions.get(it.symbol, 0.0))
            if qty <= 0:
                continue
            broker.submit_limit_order(it.symbol, it.side, qty, lim, f"bt-{d}-{it.symbol}-{it.side}",
                                      reference_price=it.reference_price)
    idx = pd.Index(dates[: len(eq)], name="date")
    eq_s, bench_s, expo_s = pd.Series(eq, index=idx), pd.Series(bench, index=idx), pd.Series(expo, index=idx)
    metrics = perf_metrics(eq_s.tolist(), bench_s.tolist() if bench_s.notna().all() else None)
    return BacktestResult(eq_s, bench_s, trades, metrics, expo_s, regime_counts, n_reb)


def walk_forward_windows(dates: List[str], years: int = 1) -> List[tuple]:
    """Contiguous yearly windows for out-of-sample reporting (no parameter fitting happens in V1,
    so 'walk-forward' here means: report each window separately and check stability)."""
    out = []
    ys = sorted({d[:4] for d in dates})
    for y in ys:
        ds = [d for d in dates if d.startswith(y)]
        if len(ds) > 20:
            out.append((ds[0], ds[-1]))
    return out
