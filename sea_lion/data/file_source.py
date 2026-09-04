"""Approved-events file source: a JSON list of {symbol, published_at, title, summary, source, url}.
Useful for deterministic tests and for feeding hand-curated events."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List

from .base import Event


class FileEvents:
    name = "file"

    def __init__(self, path: str):
        self.path = Path(path)

    def fetch_events(self, symbols: List[str], since: datetime, max_per_symbol: int) -> List[Event]:
        if not self.path.exists():
            return []
        items = json.loads(self.path.read_text())
        out: List[Event] = []
        counts: dict = {}
        for it in items:
            if it.get("symbol") not in symbols:
                continue
            ts = datetime.fromisoformat(str(it["published_at"]).replace("Z", "+00:00"))
            if ts < since:
                continue
            if counts.get(it["symbol"], 0) >= max_per_symbol:
                continue
            counts[it["symbol"]] = counts.get(it["symbol"], 0) + 1
            out.append(Event(symbol=it["symbol"], published_at=ts.isoformat(), title=it.get("title", ""),
                             summary=it.get("summary", ""), source=it.get("source", "file"), url=it.get("url", "")))
        return out


class NoEvents:
    name = "none"

    def fetch_events(self, symbols: List[str], since: datetime, max_per_symbol: int) -> List[Event]:
        return []
