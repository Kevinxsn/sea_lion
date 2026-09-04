from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Protocol

import pandas as pd

BAR_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]


class MarketData(Protocol):
    name: str

    def fetch_bars(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        """Daily bars, long format with BAR_COLUMNS; `date` is ISO string; prices split/dividend-adjusted in adj_close."""

    def latest_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Most recent trade/close price per symbol (used for pre-submit checks)."""


@dataclass
class Event:
    symbol: str
    published_at: str            # ISO 8601 UTC
    title: str
    summary: str = ""
    source: str = ""
    url: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(f"{self.symbol}|{self.title}|{self.summary}".encode()).hexdigest()[:16]

    @property
    def event_id(self) -> str:
        return f"evt_{self.symbol}_{self.content_hash}"

    def to_row(self) -> Dict[str, Any]:
        return {"event_id": self.event_id, "symbol": self.symbol, "published_at": self.published_at,
                "source": self.source, "title": self.title, "summary": self.summary, "url": self.url,
                "content_hash": self.content_hash}


class EventSource(Protocol):
    name: str

    def fetch_events(self, symbols: List[str], since: datetime, max_per_symbol: int) -> List[Event]: ...


def bars_hash(df: pd.DataFrame) -> str:
    """Stable content hash of a bars frame (used as the run input hash)."""
    if df.empty:
        return "empty"
    d = df.sort_values(["symbol", "date"])[BAR_COLUMNS].round(6)
    return hashlib.sha256(pd.util.hash_pandas_object(d, index=False).values.tobytes()).hexdigest()[:16]
