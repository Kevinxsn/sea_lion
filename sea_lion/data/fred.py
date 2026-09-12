"""FRED macro observations via the key-less CSV endpoint. Live available_at = retrieval time
(design §6.2); ALFRED vintages are a deferred research upgrade."""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, timedelta
from typing import Any, Dict, List

import httpx

from .documents import now_utc

log = logging.getLogger(__name__)


class Fred:
    def __init__(self, timeout: float = 20.0):
        self._client = httpx.Client(timeout=timeout)

    def observations(self, series: str, lookback_days: int = 400) -> List[Dict[str, Any]]:
        try:
            r = self._client.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": series})
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("FRED fetch failed for %s: %s", series, e)
            return []
        now = now_utc().isoformat(timespec="seconds")
        since = (date.today() - timedelta(days=lookback_days)).isoformat()
        out = []
        for row in csv.DictReader(io.StringIO(r.text)):
            d = row.get("observation_date") or row.get("DATE") or next(iter(row.values()))
            v = row.get(series)
            if not d or d < since or v in (None, ".", ""):
                continue
            try:
                out.append({"series": series, "effective_date": d, "value": float(v), "retrieved_at": now, "available_at": now,
                            "source": "fred"})
            except ValueError:
                continue
        return out
