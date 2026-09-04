"""Reconciliation: sync order status from the broker, expire stale orders, and compare the
broker's positions with what our own fill history implies. Any disagreement => safe mode.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from .broker.base import AmbiguousBrokerResponse, Broker
from .config import Settings
from .store import Store

log = logging.getLogger(__name__)
QTY_TOL = 1e-3


def reconcile(store: Store, broker: Broker, cfg: Settings, run_id: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"updated": 0, "canceled_stale": 0, "mismatches": [], "ok": True, "ambiguous": []}
    now = datetime.now(timezone.utc)
    expected: Dict[str, float] = dict(store.kv_get("expected_positions", {}))

    # 1) refresh every locally-open order from the broker
    for o in store.open_orders():
        try:
            bo = broker.get_order_by_client_id(o["client_order_id"])
        except AmbiguousBrokerResponse as e:
            result["ambiguous"].append({"cid": o["client_order_id"], "error": str(e)})
            continue
        if bo is None:
            o["status"] = "unknown_at_broker"
            store.upsert_order(o)
            result["mismatches"].append({"type": "order_missing_at_broker", "cid": o["client_order_id"]})
            continue
        # stale: submitted longer ago than the timeout and still open -> cancel, no chase
        if bo.is_open and o.get("submitted_at"):
            try:
                age = (now - datetime.fromisoformat(str(o["submitted_at"]).replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                age = 0
            if age > cfg.broker.order_timeout_sec and broker.name != "sim":
                broker.cancel_order(bo.broker_order_id)
                bo.status = "canceled"
                result["canceled_stale"] += 1
        row = bo.to_row(o["run_id"], o.get("notional") or 0.0)
        prev_filled = float(o.get("filled_qty") or 0.0)
        store.upsert_order(row)
        result["updated"] += 1
        new_filled = float(bo.filled_qty or 0.0) - prev_filled
        if new_filled > 0:
            sign = 1.0 if bo.side == "buy" else -1.0
            expected[bo.symbol] = expected.get(bo.symbol, 0.0) + sign * new_filled

    # 2) broker positions vs expected
    acct = broker.account()
    broker_pos = {s: p.qty for s, p in acct.positions.items()}
    for sym in set(expected) | set(broker_pos):
        e, b = expected.get(sym, 0.0), broker_pos.get(sym, 0.0)
        if abs(e - b) > QTY_TOL:
            result["mismatches"].append({"type": "position_mismatch", "symbol": sym, "expected": e, "broker": b})
    if result["mismatches"] or result["ambiguous"]:
        result["ok"] = False
        store.enter_safe_mode(f"reconciliation mismatch: {result['mismatches'][:3]} {result['ambiguous'][:3]}", run_id)
    else:
        # Once reconciled, the broker snapshot becomes the new expected baseline.
        store.kv_set("expected_positions", {s: q for s, q in broker_pos.items() if abs(q) > QTY_TOL})
    store.save_reconciliation(run_id, result["ok"], result["mismatches"] + result["ambiguous"])
    return result


def accept_broker_state(store: Store, broker: Broker) -> Dict[str, float]:
    """Manual override after human review: take the broker's positions as the truth."""
    pos = {s: p.qty for s, p in broker.account().positions.items()}
    store.kv_set("expected_positions", pos)
    return pos
