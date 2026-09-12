"""Canonical-event clustering across sources, symbols, batches, and daily runs (design §7.1 steps 6–7).

Deterministic: an incoming (symbol, event_type, title/body, time) is merged into an existing canonical
event when it is the same real-world event (fingerprint match or high text overlap within a time
window); otherwise a new event is created with a novelty score relative to the symbol's recent
events. Supersession links a materially updated event of the same type to its predecessor."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..data.documents import fingerprint, similarity
from ..store import Store


# Event types that occur once per window for a company: filings and news about the same release cluster by type.
SINGULAR_TYPES = {"earnings", "quarterly_report", "annual_report", "guidance"}


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class EventClusterer:
    def __init__(self, store: Store, window_days: int = 3, merge_threshold: float = 0.5, novelty_days: int = 30):
        self.store = store
        self.window = timedelta(days=window_days)
        self.merge_threshold = merge_threshold
        self.novelty_days = novelty_days
        self._cache: Dict[str, List[Dict[str, Any]]] = {}

    def _recent(self, symbol: str, available_at: str) -> List[Dict[str, Any]]:
        key = f"{symbol}|{available_at[:10]}"
        if key not in self._cache:
            since = (_dt(available_at) - timedelta(days=self.novelty_days)).isoformat()
            until = (_dt(available_at) + self.window).isoformat()
            self._cache[key] = [e for e in self.store.events(since, until) if e["primary_symbol"] == symbol]
        return self._cache[key]

    def assign(self, symbol: str, event_type: str, title: str, body: str, available_at: str, doc_id: str,
               claim_ids: List[str], importance: float, sentiment: float, source_quality: float, run_id: str,
               related_symbols: Optional[List[str]] = None, event_time: Optional[str] = None
               ) -> Tuple[Dict[str, Any], str]:
        """Returns (event_row, outcome) with outcome in {new, merged, superseding}."""
        fp = fingerprint(title)
        best, best_sim = None, 0.0
        for e in self._recent(symbol, available_at):
            if abs(_dt(e["available_at"]) - _dt(available_at)) > self.window:
                continue
            if e["event_type"] != event_type and not (e["fingerprint"] == fp):
                continue
            if e["event_type"] == event_type and event_type in SINGULAR_TYPES:
                sim = 1.0            # a company has one earnings release per window: same type => same event
            else:
                sim = 1.0 if e["fingerprint"] == fp else similarity(title, body, e["title"] or "", "")
            if sim > best_sim:
                best, best_sim = e, sim
        if best is not None and best_sim >= self.merge_threshold:
            docs = list(dict.fromkeys(best["document_ids"] + [doc_id]))
            claims = list(dict.fromkeys(best["claim_ids"] + claim_ids))
            row = dict(best, document_ids=docs, claim_ids=claims, status="updated" if len(docs) > 1 else best["status"],
                       importance=max(float(best.get("importance") or 0), importance),
                       source_quality=max(float(best.get("source_quality") or 0), source_quality),
                       sentiment=float(best.get("sentiment") or sentiment), available_at=min(best["available_at"], available_at),
                       related_symbols=sorted(set(best["related_symbols"]) | set(related_symbols or [])),
                       novelty=float(best.get("novelty") or 0.0), run_id=run_id)
            self.store.upsert_event(row)
            self._cache.clear()
            return row, "merged"
        # novelty: 1 - max similarity to anything this symbol had in the last 30 days
        max_sim = 0.0
        supersedes = None
        for e in self._recent(symbol, available_at):
            if _dt(e["available_at"]) > _dt(available_at):
                continue
            sim = similarity(title, body, e["title"] or "", "")
            max_sim = max(max_sim, sim)
            if e["event_type"] == event_type and 0.25 <= sim < self.merge_threshold:
                supersedes = e["event_id"]
        novelty = round(1.0 - max_sim, 3)
        eid = "evt_" + available_at[:10].replace("-", "") + "_" + symbol.lower().replace("-", "") + "_" + event_type[:12] + "_" + \
              hashlib.sha1(f"{symbol}|{event_type}|{fp}".encode()).hexdigest()[:6]
        row = {"event_id": eid, "event_type": event_type, "primary_symbol": symbol, "related_symbols": sorted(set(related_symbols or [])),
               "event_time": event_time or available_at, "available_at": available_at, "status": "candidate", "novelty": novelty,
               "source_quality": source_quality, "contradiction_level": None, "importance": importance, "sentiment": sentiment,
               "fingerprint": fp, "title": title[:300], "document_ids": [doc_id], "claim_ids": list(claim_ids),
               "supersedes_event_id": supersedes, "run_id": run_id}
        self.store.upsert_event(row)
        self._cache.clear()
        return row, ("superseding" if supersedes else "new")
