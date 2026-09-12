"""Execution: risk-approved intents -> idempotent broker orders.

- client_order_id is a deterministic hash of (run_id, symbol, side, strategy_version).
- Before every submission the important checks are repeated with fresh values.
- An order that already exists (locally or at the broker) is never resubmitted.
- Ambiguous broker responses are queried by client_order_id, never blindly retried.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .broker.base import AmbiguousBrokerResponse, Broker, BrokerError
from .config import Settings
from .risk import OrderIntent, presubmit_check
from .store import Store

log = logging.getLogger(__name__)


def client_order_id(run_id: str, symbol: str, side: str, strategy_version: str) -> str:
    h = hashlib.sha256(f"{run_id}|{symbol}|{side}|{strategy_version}".encode()).hexdigest()[:24]
    return f"sl-{h}"


def limit_price(reference: float, side: str, offset_bps: float) -> float:
    k = offset_bps / 1e4
    px = reference * (1 + k) if side == "buy" else reference * (1 - k)
    return round(px, 2)


@dataclass
class ExecutionReport:
    submitted: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    ambiguous: List[Dict[str, Any]] = field(default_factory=list)
    duplicates_prevented: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__


def execute(intents: List[OrderIntent], broker: Broker, store: Store, cfg: Settings, run_id: str,
            fresh_prices: Dict[str, float], market_open_next: bool) -> ExecutionReport:
    rep = ExecutionReport()
    if "limit" not in cfg.broker.allowed_order_types:
        raise BrokerError("limit orders not on the allowlist; nothing else is implemented")
    acct = broker.account()
    cash = acct.cash
    for it in intents:
        cid = client_order_id(run_id, it.symbol, it.side, cfg.run.strategy_version)
        # ---- idempotency: local record first, then broker -----------------
        local = store.order(cid)
        if local and local["status"] not in ("rejected_local",):
            rep.duplicates_prevented += 1
            rep.skipped.append({"symbol": it.symbol, "cid": cid, "why": f"already_recorded:{local['status']}"})
            continue
        try:
            remote = broker.get_order_by_client_id(cid)
        except AmbiguousBrokerResponse as e:
            rep.ambiguous.append({"symbol": it.symbol, "cid": cid, "why": str(e)})
            continue
        if remote is not None:
            rep.duplicates_prevented += 1
            store.upsert_order(remote.to_row(run_id, it.notional))
            rep.skipped.append({"symbol": it.symbol, "cid": cid, "why": f"exists_at_broker:{remote.status}"})
            continue
        # ---- fresh pre-submit checks ---------------------------------------
        fresh = fresh_prices.get(it.symbol, 0.0)
        pos_qty = acct.positions[it.symbol].qty if it.symbol in acct.positions else 0.0
        why = presubmit_check(it, fresh, cash, pos_qty, acct.equity, cfg.risk, market_open_next)
        if why:
            store.upsert_order({"client_order_id": cid, "run_id": run_id, "symbol": it.symbol, "side": it.side,
                                "qty": it.qty, "notional": it.notional, "limit_price": None, "status": "rejected_local",
                                "raw": {"reason": why}})
            rep.skipped.append({"symbol": it.symbol, "cid": cid, "why": why})
            continue
        lim = limit_price(fresh, it.side, cfg.broker.limit_offset_bps)
        qty = round(it.notional / lim, 4)
        if it.side == "sell":
            # closing: use the broker's exact held quantity so no dust is left behind
            qty = pos_qty if it.close_position else min(qty, round(pos_qty, 4))
        if qty <= 0:
            rep.skipped.append({"symbol": it.symbol, "cid": cid, "why": "zero_qty"})
            continue
        # ---- submit ----------------------------------------------------------
        store.upsert_order({"client_order_id": cid, "run_id": run_id, "symbol": it.symbol, "side": it.side,
                            "qty": qty, "notional": it.notional, "limit_price": lim, "status": "new",
                            "raw": {"reason": it.reason}})
        try:
            if getattr(broker, "name", "") == "sim":
                bo = broker.submit_limit_order(it.symbol, it.side, qty, lim, cid, reference_price=fresh)
            else:
                bo = broker.submit_limit_order(it.symbol, it.side, qty, lim, cid)
        except AmbiguousBrokerResponse as e:
            store.upsert_order({"client_order_id": cid, "run_id": run_id, "symbol": it.symbol, "side": it.side,
                                "qty": qty, "notional": it.notional, "limit_price": lim, "status": "ambiguous",
                                "raw": {"error": str(e)}})
            rep.ambiguous.append({"symbol": it.symbol, "cid": cid, "why": str(e)})
            continue
        except BrokerError as e:
            store.upsert_order({"client_order_id": cid, "run_id": run_id, "symbol": it.symbol, "side": it.side,
                                "qty": qty, "notional": it.notional, "limit_price": lim, "status": "rejected",
                                "raw": {"error": str(e)}})
            rep.errors.append({"symbol": it.symbol, "cid": cid, "why": str(e)})
            continue
        row = bo.to_row(run_id, it.notional)
        row["status"] = bo.status if bo.status else "submitted"
        store.upsert_order(row)
        if it.side == "buy":
            cash -= it.notional
        rep.submitted.append({"symbol": it.symbol, "cid": cid, "side": it.side, "qty": qty, "limit": lim,
                              "notional": round(it.notional, 2), "status": row["status"]})
    return rep


def kill_switch(broker: Broker, store: Store, reason: str, run_id: Optional[str] = None) -> Dict[str, Any]:
    """One action: cancel every open order and block new exposure (safe mode). No liquidation."""
    n = broker.cancel_all()
    for o in store.open_orders():
        o["status"] = "canceled"
        store.upsert_order(o)
    store.enter_safe_mode(f"kill_switch: {reason}", run_id)
    return {"canceled": n, "safe_mode": True}


def sweep_dust(broker: Broker, store: Store, cfg: Settings, run_id: str, confirm: bool = False) -> Dict[str, Any]:
    """Idempotent maintenance (design §14): list positions worth less than the minimum order size;
    close them only with confirm=True (exact-quantity close), recording each cleanup."""
    acct = broker.account()
    dust = [{"symbol": s, "qty": p.qty, "value": round(p.market_value, 2)} for s, p in acct.positions.items()
            if 0 < p.market_value < cfg.risk.min_order_notional]
    out: Dict[str, Any] = {"candidates": dust, "closed": [], "errors": [], "confirmed": confirm}
    if not confirm:
        return out
    for d in dust:
        cid = client_order_id(run_id, d["symbol"], "sell", "dust-sweep")
        if store.order(cid):
            out["closed"].append({**d, "cid": cid, "note": "already_recorded"})
            continue
        try:
            close = getattr(broker, "close_position", None)
            if close is None:
                raise BrokerError("broker has no close_position")
            bo = close(d["symbol"], cid)
            row = bo.to_row(run_id, d["value"]); row["raw"] = {"reason": "dust_sweep"}
            store.upsert_order(row)
            store.add_health_event("info", "dust_sweep", f"closed dust {d['symbol']} qty {d['qty']}", run_id)
            out["closed"].append({**d, "cid": cid, "status": bo.status})
        except Exception as e:  # noqa: BLE001
            out["errors"].append({**d, "error": str(e)[:200]})
    return out
