"""Alpaca paper/live adapter via alpaca-py. Paper and live are chosen by the `paper` flag and
use different credentials (see Settings.alpaca_keys). Not exercised until keys are provided."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import Account, AmbiguousBrokerResponse, BrokerError, BrokerOrder, Position

log = logging.getLogger(__name__)


def _to_alpaca(sym: str) -> str:
    return sym.replace("-", ".")


def _from_alpaca(sym: str) -> str:
    return sym.replace(".", "-")


class AlpacaBroker:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        if not api_key or not secret_key:
            raise BrokerError("Alpaca credentials missing (ALPACA_PAPER_API_KEY / ALPACA_PAPER_SECRET_KEY)")
        from alpaca.trading.client import TradingClient
        self.paper = paper
        self.name = "alpaca_paper" if paper else "alpaca_live"
        self._c = TradingClient(api_key, secret_key, paper=paper)

    def account(self) -> Account:
        a = self._c.get_account()
        pos = {}
        for p in self._c.get_all_positions():
            sym = _from_alpaca(p.symbol)
            pos[sym] = Position(sym, float(p.qty), float(p.avg_entry_price), float(p.current_price))
        return Account(equity=float(a.equity), cash=float(a.cash), buying_power=float(a.buying_power),
                       positions=pos, last_equity=float(a.last_equity) if a.last_equity else None,
                       raw={"status": str(a.status), "trading_blocked": bool(a.trading_blocked),
                            "account_blocked": bool(a.account_blocked), "multiplier": str(a.multiplier)})

    def clock(self) -> Dict[str, Any]:
        c = self._c.get_clock()
        return {"is_open": bool(c.is_open), "next_open": str(c.next_open), "next_close": str(c.next_close),
                "timestamp": str(c.timestamp), "source": "alpaca"}

    def submit_limit_order(self, symbol: str, side: str, qty: float, limit_price: float,
                           client_order_id: str) -> BrokerOrder:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest
        existing = self.get_order_by_client_id(client_order_id)
        if existing is not None:
            return existing                       # idempotent: never submit twice
        req = LimitOrderRequest(symbol=_to_alpaca(symbol), qty=round(qty, 4),
                                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
                                client_order_id=client_order_id)
        try:
            o = self._c.submit_order(req)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            # 4xx = the broker understood and rejected: safe to report. Anything else is ambiguous.
            if any(t in msg for t in ("400", "403", "422", "insufficient", "not allowed", "invalid")):
                raise BrokerError(f"rejected: {msg[:300]}") from e
            check = self.get_order_by_client_id(client_order_id)
            if check is not None:
                return check
            raise AmbiguousBrokerResponse(msg[:300]) from e
        return self._conv(o)

    def get_order_by_client_id(self, client_order_id: str) -> Optional[BrokerOrder]:
        try:
            o = self._c.get_order_by_client_id(client_order_id)
        except Exception as e:  # noqa: BLE001
            if "404" in str(e) or "not found" in str(e).lower():
                return None
            raise AmbiguousBrokerResponse(str(e)[:300]) from e
        return self._conv(o)

    def open_orders(self) -> List[BrokerOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        return [self._conv(o) for o in self._c.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))]

    def cancel_order(self, broker_order_id: str) -> None:
        self._c.cancel_order_by_id(broker_order_id)

    def cancel_all(self) -> int:
        res = self._c.cancel_orders()
        return len(res) if res else 0

    def close_position(self, symbol: str, client_order_id: str) -> BrokerOrder:
        """Exact-quantity liquidation of one position (used only by the audited dust sweep)."""
        o = self._c.close_position(_to_alpaca(symbol))
        bo = self._conv(o)
        bo.client_order_id = client_order_id if not bo.client_order_id else bo.client_order_id
        return bo

    @staticmethod
    def _conv(o: Any) -> BrokerOrder:
        return BrokerOrder(client_order_id=str(o.client_order_id), symbol=_from_alpaca(str(o.symbol)),
                           side=str(o.side).split(".")[-1].lower(), qty=float(o.qty or 0),
                           limit_price=float(o.limit_price) if o.limit_price else None,
                           status=str(o.status).split(".")[-1].lower(), broker_order_id=str(o.id),
                           filled_qty=float(o.filled_qty or 0),
                           filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
                           submitted_at=str(o.submitted_at or ""), raw={"type": str(o.type), "tif": str(o.time_in_force)})
