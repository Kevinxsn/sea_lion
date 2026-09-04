from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

OPEN_STATUSES = {"new", "accepted", "submitted", "partially_filled", "pending_new", "accepted_for_bidding", "held"}
TERMINAL_STATUSES = {"filled", "canceled", "cancelled", "expired", "rejected", "replaced", "done_for_day", "stopped", "suspended"}


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    current_price: float

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float
    positions: Dict[str, Position]
    last_equity: Optional[float] = None    # previous close equity (start of day)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerOrder:
    client_order_id: str
    symbol: str
    side: str
    qty: float
    limit_price: Optional[float]
    status: str
    broker_order_id: str = ""
    filled_qty: float = 0.0
    filled_avg_price: Optional[float] = None
    fees: float = 0.0
    submitted_at: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def to_row(self, run_id: str, notional: float) -> Dict[str, Any]:
        return {"client_order_id": self.client_order_id, "run_id": run_id, "symbol": self.symbol, "side": self.side,
                "qty": self.qty, "notional": notional, "limit_price": self.limit_price, "order_type": "limit",
                "status": self.status, "broker_order_id": self.broker_order_id, "submitted_at": self.submitted_at,
                "filled_qty": self.filled_qty, "filled_avg_price": self.filled_avg_price, "fees": self.fees,
                "raw": self.raw}


class Broker(Protocol):
    name: str

    def account(self) -> Account: ...
    def clock(self) -> Dict[str, Any]: ...
    def submit_limit_order(self, symbol: str, side: str, qty: float, limit_price: float,
                           client_order_id: str) -> BrokerOrder: ...
    def get_order_by_client_id(self, client_order_id: str) -> Optional[BrokerOrder]: ...
    def open_orders(self) -> List[BrokerOrder]: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
    def cancel_all(self) -> int: ...


class BrokerError(RuntimeError):
    pass


class AmbiguousBrokerResponse(BrokerError):
    """Raised when we cannot tell whether an order was accepted. Never retry blindly."""
