"""Deterministic risk engine. Pure functions; the final authority before any order.

Input: a proposed target-weight map plus fresh account state. Output: approved weights,
order intents, and a machine-readable reason for every reduction or rejection.
Nothing here calls a network or a model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import RiskCfg


@dataclass
class AccountState:
    equity: float
    cash: float
    positions: Dict[str, float]          # symbol -> quantity
    prices: Dict[str, float]             # symbol -> last price
    start_of_day_equity: float
    high_water_mark: float
    data_stale: bool = False
    safe_mode: bool = False
    market_open_next_session: bool = True

    def position_value(self, sym: str) -> float:
        return self.positions.get(sym, 0.0) * self.prices.get(sym, 0.0)

    def weights(self) -> Dict[str, float]:
        if self.equity <= 0:
            return {}
        return {s: self.position_value(s) / self.equity for s in self.positions if self.positions[s] != 0}


@dataclass
class OrderIntent:
    symbol: str
    side: str                 # buy | sell
    notional: float           # positive dollars
    reference_price: float
    reason: str = "rebalance"
    close_position: bool = False   # sell the ENTIRE held quantity (target ~0): avoids dust from qty rounding

    @property
    def qty(self) -> float:
        return self.notional / self.reference_price if self.reference_price > 0 else 0.0


@dataclass
class RiskDecision:
    symbol: str
    requested_weight: float
    approved_weight: float
    reasons: List[str] = field(default_factory=list)


@dataclass
class RiskResult:
    approved: Dict[str, float]
    intents: List[OrderIntent]                 # will be submitted
    decisions: List[RiskDecision]
    global_flags: List[str]
    block_new_exposure: bool
    enter_safe_mode: Optional[str] = None
    stats: Dict[str, float] = field(default_factory=dict)
    held_for_review: List[OrderIntent] = field(default_factory=list)   # risk-reducing trades needing a human

    def to_dict(self) -> Dict:
        return {"approved": self.approved, "intents": [i.__dict__ for i in self.intents],
                "decisions": [d.__dict__ for d in self.decisions], "global_flags": self.global_flags,
                "block_new_exposure": self.block_new_exposure, "enter_safe_mode": self.enter_safe_mode,
                "stats": self.stats, "held_for_review": [i.__dict__ for i in self.held_for_review]}


def evaluate(proposal: Dict[str, float], acct: AccountState, sectors: Dict[str, str], cfg: RiskCfg,
             rank_order: Optional[List[str]] = None) -> RiskResult:
    flags: List[str] = []
    block_new = False        # buys blocked
    hold_sells = False       # sells not auto-submitted; listed for manual review (safe mode)
    no_orders = False        # nothing may be generated (stale data / bad account)
    enter_safe: Optional[str] = None
    decisions: Dict[str, RiskDecision] = {s: RiskDecision(s, w, w) for s, w in proposal.items()}

    def note(sym: str, w: float, why: str) -> None:
        d = decisions.setdefault(sym, RiskDecision(sym, proposal.get(sym, 0.0), w))
        d.approved_weight = w
        d.reasons.append(why)

    # ---- global gates -------------------------------------------------------
    if acct.equity <= 0:
        flags.append("nonpositive_equity")
        block_new = no_orders = True
    if acct.data_stale:
        flags.append("data_stale")
        block_new = no_orders = True
    if acct.safe_mode:
        flags.append("safe_mode_active")
        block_new = hold_sells = True
    dd = 0.0 if acct.high_water_mark <= 0 else (acct.high_water_mark - acct.equity) / acct.high_water_mark
    if dd >= cfg.drawdown_safe_mode:
        flags.append(f"drawdown_{dd:.3f}>={cfg.drawdown_safe_mode}")
        block_new = hold_sells = True
        enter_safe = f"portfolio drawdown {dd:.1%} from high-water mark"
    day_pnl = 0.0 if acct.start_of_day_equity <= 0 else (acct.equity - acct.start_of_day_equity) / acct.start_of_day_equity
    if day_pnl <= -cfg.daily_loss_lock:
        flags.append(f"daily_loss_lock_{day_pnl:.3f}")
        block_new = True
    if not acct.market_open_next_session:
        flags.append("market_closed")
        no_orders = True
    explicit_zero = {s for s, w in proposal.items() if w == 0}

    current = acct.weights()
    approved: Dict[str, float] = {}

    # ---- per-position limits -----------------------------------------------
    order = rank_order or sorted(proposal, key=lambda s: -proposal[s])
    for sym in order:
        w = float(proposal[sym])
        if w < 0:
            note(sym, 0.0, "short_not_allowed")
            continue
        if sym not in sectors:
            note(sym, 0.0, "symbol_not_in_universe")
            continue
        if sym not in acct.prices or acct.prices[sym] <= 0:
            note(sym, 0.0, "no_price")
            continue
        if w > cfg.single_position_max:
            note(sym, cfg.single_position_max, f"clamp_single_position_{cfg.single_position_max}")
            w = cfg.single_position_max
        approved[sym] = w

    # ---- position count -----------------------------------------------------
    if len(approved) > cfg.max_positions:
        keep = [s for s in order if s in approved][: cfg.max_positions]
        for s in list(approved):
            if s not in keep:
                note(s, 0.0, f"max_positions_{cfg.max_positions}")
                del approved[s]

    # ---- sector concentration (reduce the newest / lowest-ranked) ----------
    sector_tot: Dict[str, float] = {}
    for s in [x for x in order if x in approved]:
        sec = sectors.get(s, "Unknown")
        room = cfg.sector_max - sector_tot.get(sec, 0.0)
        if approved[s] > room + 1e-9:
            neww = max(room, 0.0)
            note(s, neww, f"sector_cap_{sec}_{cfg.sector_max}")
            approved[s] = neww
        sector_tot[sec] = sector_tot.get(sec, 0.0) + approved[s]
    approved = {s: w for s, w in approved.items() if w > 0}

    # ---- gross exposure -----------------------------------------------------
    gross = sum(approved.values())
    if gross > cfg.gross_exposure_max + 1e-9:
        k = cfg.gross_exposure_max / gross
        for s in approved:
            approved[s] = approved[s] * k
            note(s, approved[s], f"gross_scaled_{k:.3f}")

    # ---- block new exposure: targets may only go down --------------------
    if block_new:
        for s in list(approved):
            cur = current.get(s, 0.0)
            if approved[s] > cur:
                note(s, cur, "new_exposure_blocked")
                approved[s] = cur
        for s in current:
            if s not in explicit_zero:
                approved.setdefault(s, current[s])   # untouched holdings stay (no forced liquidation)
        approved = {s: w for s, w in approved.items() if w > 0}

    # ---- order intents ------------------------------------------------------
    intents: List[OrderIntent] = []
    all_syms = set(approved) | set(current)
    for s in sorted(all_syms):
        px = acct.prices.get(s, 0.0)
        if px <= 0:
            continue
        delta = (approved.get(s, 0.0) - current.get(s, 0.0)) * acct.equity
        if abs(delta) < cfg.min_order_notional:
            if s in decisions and abs(delta) > 0:
                decisions[s].reasons.append(f"skip_dust_{abs(delta):.2f}")
            continue
        if delta > 0 and block_new:
            note(s, current.get(s, 0.0), "buy_blocked")
            continue
        if delta > 0:
            delta = min(delta, cfg.max_order_notional_frac * acct.equity)
        else:
            # sells: can't sell more than we hold
            delta = -min(-delta, acct.position_value(s))
        # if what would remain is below the minimum order size, close the whole position instead
        remaining = acct.position_value(s) + delta
        close = delta < 0 and remaining < cfg.min_order_notional
        intents.append(OrderIntent(s, "buy" if delta > 0 else "sell", abs(delta), px, close_position=close))

    # ---- turnover: scale non-risk-reducing (buy) intents ------------------
    turnover = sum(i.notional for i in intents) / acct.equity if acct.equity > 0 else 0.0
    if turnover > cfg.turnover_max + 1e-9:
        sells = sum(i.notional for i in intents if i.side == "sell")
        buys = sum(i.notional for i in intents if i.side == "buy")
        allowed_buys = max(cfg.turnover_max * acct.equity - sells, 0.0)
        k = allowed_buys / buys if buys > 0 else 0.0
        kept: List[OrderIntent] = []
        for i in intents:
            if i.side == "buy":
                i.notional = i.notional * k
                i.reason = f"turnover_scaled_{k:.3f}"
                if i.notional < cfg.min_order_notional:
                    note(i.symbol, current.get(i.symbol, 0.0), "turnover_dropped")
                    continue
                note(i.symbol, current.get(i.symbol, 0.0) + i.notional / acct.equity, f"turnover_scaled_{k:.3f}")
            kept.append(i)
        intents = kept
        flags.append(f"turnover_capped_{turnover:.3f}")

    # ---- buying power: buys funded by cash + same-day sell proceeds ----------
    sell_proceeds = sum(i.notional for i in intents if i.side == "sell")
    buy_total = sum(i.notional for i in intents if i.side == "buy")
    available = max(acct.cash, 0.0) + sell_proceeds
    if buy_total > available + 1e-6:
        k = available / buy_total if buy_total > 0 else 0.0
        kept = []
        for i in intents:
            if i.side == "buy":
                i.notional *= k
                if i.notional < cfg.min_order_notional:
                    note(i.symbol, current.get(i.symbol, 0.0), "insufficient_cash_dropped")
                    continue
                note(i.symbol, current.get(i.symbol, 0.0) + i.notional / acct.equity, f"cash_scaled_{k:.3f}")
            kept.append(i)
        intents = kept
        flags.append("buying_power_limited")

    # sells first so proceeds exist before buys are submitted
    intents.sort(key=lambda i: (0 if i.side == "sell" else 1, i.symbol))
    held: List[OrderIntent] = []
    if no_orders:
        held, intents = intents, []
        flags.append("all_orders_blocked")
    elif hold_sells:
        held = [i for i in intents if i.side == "sell"]
        intents = [i for i in intents if i.side != "sell"]
        if held:
            flags.append("sells_held_for_manual_review")
    stats = {"gross_target": sum(approved.values()), "n_positions": len(approved), "turnover": turnover,
             "drawdown": dd, "day_pnl": day_pnl, "cash_before": acct.cash, "equity": acct.equity}
    return RiskResult(approved={s: round(w, 6) for s, w in approved.items()}, intents=intents,
                      decisions=list(decisions.values()), global_flags=flags, block_new_exposure=block_new,
                      enter_safe_mode=enter_safe, stats=stats, held_for_review=held)


def presubmit_check(intent: OrderIntent, fresh_price: float, cash: float, position_qty: float,
                    equity: float, cfg: RiskCfg, market_open: bool) -> Optional[str]:
    """Repeat the important checks with fresh values immediately before submission (design §8)."""
    if not market_open:
        return "market_closed"
    if fresh_price <= 0:
        return "no_fresh_price"
    drift = abs(fresh_price / intent.reference_price - 1.0) if intent.reference_price > 0 else 1.0
    if drift > 0.05:
        return f"price_moved_{drift:.3f}"
    if intent.notional < cfg.min_order_notional:
        return "below_min_notional"
    if intent.notional > cfg.max_order_notional_frac * equity + 1e-6:
        return "above_max_notional"
    if intent.side == "buy" and intent.notional > cash + 1e-6:
        return "insufficient_buying_power"
    if intent.side == "sell" and intent.notional > position_qty * fresh_price * 1.02 + 1e-6:
        return "sell_exceeds_position"
    return None
