"""Local simulated broker.

Models the intended live flow: decide on session T's close, submit a marketable limit
order shortly after the open of T+1 with a fresh reference price. Fills therefore happen at
T+1's open plus slippage. If the open has gapped more than `max_gap` from the decision
reference the pre-submit drift check would have refused the order, so it expires unfilled
(no chasing). State is a plain dict so the same class drives the daily `sim` mode
(persisted in SQLite) and the in-memory backtest.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .base import Account, BrokerError, BrokerOrder, Position


def empty_state(cash: float) -> Dict[str, Any]:
    return {"cash": float(cash), "positions": {}, "orders": {}, "prices": {}, "last_equity": float(cash),
            "fills": [], "date": None}


class SimBroker:
    name = "sim"

    def __init__(self, state: Dict[str, Any], slippage_bps: float = 5.0, commission_usd: float = 0.0,
                 persist: Optional[Callable[[Dict[str, Any]], None]] = None, max_gap: float = 0.05):
        self.s = state
        self.slip = slippage_bps / 1e4
        self.commission = commission_usd
        self.max_gap = max_gap
        self._persist = persist or (lambda st: None)

    # ---- market simulation ------------------------------------------------
    def settle(self, date: str, opens: Dict[str, float], closes: Dict[str, float]) -> List[BrokerOrder]:
        """Fill open orders at today's open, then mark to today's close. Called once per session."""
        self.s["last_equity"] = self.equity()
        filled: List[BrokerOrder] = []
        # sells first, then buys (so buys can use proceeds)
        for cid, o in sorted(self.s["orders"].items(), key=lambda kv: (0 if kv[1]["side"] == "sell" else 1, kv[0])):
            if o["status"] not in ("accepted", "new"):
                continue
            px = opens.get(o["symbol"])
            if px is None:
                o["status"] = "expired"
                continue
            fill_px = px * (1 + self.slip) if o["side"] == "buy" else px * (1 - self.slip)
            ref = o.get("reference_price")
            if ref and abs(px / ref - 1.0) > self.max_gap:
                o["status"] = "expired"          # pre-submit drift check would have refused it
                o["reason"] = f"gap_{px / ref - 1.0:+.3f}"
                continue
            qty = o["qty"]
            cost = qty * fill_px + self.commission
            pos = self.s["positions"].get(o["symbol"], {"qty": 0.0, "avg_price": 0.0})
            if o["side"] == "buy":
                if cost > self.s["cash"] + 1e-9:
                    # partial fill down to available cash (never negative cash)
                    qty = max((self.s["cash"] - self.commission) / fill_px, 0.0)
                    if qty * fill_px < 1.0:
                        o["status"] = "rejected"
                        o["reason"] = "insufficient_cash"
                        continue
                    cost = qty * fill_px + self.commission
                new_qty = pos["qty"] + qty
                pos["avg_price"] = (pos["qty"] * pos["avg_price"] + qty * fill_px) / new_qty if new_qty > 0 else 0.0
                pos["qty"] = new_qty
                self.s["cash"] -= cost
            else:
                qty = min(qty, pos["qty"])
                if qty <= 0:
                    o["status"] = "rejected"
                    o["reason"] = "no_position"
                    continue
                pos["qty"] -= qty
                self.s["cash"] += qty * fill_px - self.commission
            if pos["qty"] > 1e-9:
                self.s["positions"][o["symbol"]] = pos
            else:
                self.s["positions"].pop(o["symbol"], None)
            o["status"] = "filled" if abs(qty - o["qty"]) < 1e-9 else "partially_filled_closed"
            o["filled_qty"] = qty
            o["filled_avg_price"] = fill_px
            o["fees"] = self.commission
            o["filled_at"] = date
            self.s["fills"].append({"date": date, "client_order_id": cid, "symbol": o["symbol"], "side": o["side"],
                                    "qty": qty, "price": fill_px})
            filled.append(self._to_order(o))
        self.s["prices"].update({k: float(v) for k, v in closes.items()})
        self.s["date"] = date
        self._persist(self.s)
        return filled

    def mark(self, closes: Dict[str, float], date: Optional[str] = None) -> None:
        self.s["prices"].update({k: float(v) for k, v in closes.items()})
        if date:
            self.s["date"] = date
        self._persist(self.s)

    def equity(self) -> float:
        return self.s["cash"] + sum(p["qty"] * self.s["prices"].get(sym, p["avg_price"])
                                    for sym, p in self.s["positions"].items())

    # ---- Broker protocol ----------------------------------------------------
    def account(self) -> Account:
        pos = {sym: Position(sym, p["qty"], p["avg_price"], self.s["prices"].get(sym, p["avg_price"]))
               for sym, p in self.s["positions"].items()}
        eq = self.equity()
        return Account(equity=eq, cash=self.s["cash"], buying_power=self.s["cash"], positions=pos,
                       last_equity=self.s.get("last_equity", eq))

    def clock(self) -> Dict[str, Any]:
        return {"is_open": False, "next_session": True, "source": "sim"}

    def submit_limit_order(self, symbol: str, side: str, qty: float, limit_price: float,
                           client_order_id: str, reference_price: Optional[float] = None) -> BrokerOrder:
        if client_order_id in self.s["orders"]:
            return self._to_order(self.s["orders"][client_order_id])   # idempotent
        if qty <= 0 or limit_price <= 0:
            raise BrokerError("invalid qty/limit")
        o = {"client_order_id": client_order_id, "broker_order_id": f"sim-{len(self.s['orders']) + 1:06d}",
             "symbol": symbol, "side": side, "qty": float(qty), "limit_price": float(limit_price),
             "reference_price": float(reference_price) if reference_price else None, "status": "accepted", "filled_qty": 0.0, "filled_avg_price": None, "fees": 0.0,
             "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.s["orders"][client_order_id] = o
        self._persist(self.s)
        return self._to_order(o)

    def get_order_by_client_id(self, client_order_id: str) -> Optional[BrokerOrder]:
        o = self.s["orders"].get(client_order_id)
        return self._to_order(o) if o else None

    def open_orders(self) -> List[BrokerOrder]:
        return [self._to_order(o) for o in self.s["orders"].values() if o["status"] in ("accepted", "new")]

    def cancel_order(self, broker_order_id: str) -> None:
        for o in self.s["orders"].values():
            if o["broker_order_id"] == broker_order_id and o["status"] in ("accepted", "new"):
                o["status"] = "canceled"
        self._persist(self.s)

    def cancel_all(self) -> int:
        n = 0
        for o in self.s["orders"].values():
            if o["status"] in ("accepted", "new"):
                o["status"] = "canceled"
                n += 1
        self._persist(self.s)
        return n

    @staticmethod
    def _to_order(o: Dict[str, Any]) -> BrokerOrder:
        st = o["status"]
        if st == "partially_filled_closed":
            st = "filled"
        return BrokerOrder(client_order_id=o["client_order_id"], symbol=o["symbol"], side=o["side"], qty=o["qty"],
                           limit_price=o["limit_price"], status=st, broker_order_id=o["broker_order_id"],
                           filled_qty=o.get("filled_qty", 0.0), filled_avg_price=o.get("filled_avg_price"),
                           fees=o.get("fees", 0.0), submitted_at=o.get("submitted_at", ""), raw=dict(o))
