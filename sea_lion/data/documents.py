"""Point-in-time source documents (design §6.2–6.3, §7.1 steps 1–4).

Every document carries event_time / published_at / retrieved_at / available_at / effective_date
and a content hash. Content bodies live in compressed files under runtime/data/docs; the store
keeps the index and hash. Exact and near duplicates are detected before anything is analyzed."""
from __future__ import annotations

import gzip
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^a-z0-9 ]+")
_STOP = set("the a an and or of to in on for with by at from as is are was were be been this that these those it its into "
            "over under vs versus after before about up down new says said say stock stocks shares share market markets "
            "inc corp co ltd plc company".split())
LOW_QUALITY_TITLE = re.compile(r"(if you invested|would have this much|top \d+|\d+ stocks? to|here'?s why|what to know|"
                               r"should you buy|is it too late|reasons? to|prediction|options activity|unusual options|"
                               r"whale|insider (buys|sells)|technical analysis|price target roundup|stocks moving|"
                               r"premarket|after.?hours movers|what you need to know)", re.I)

SOURCE_QUALITY = {"sec": 0.95, "alpaca_benzinga": 0.70, "alpaca": 0.65, "yahoo": 0.50, "fred": 0.90, "file": 0.60}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | str | None) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def content_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(p or "" for p in parts).encode()).hexdigest()[:20]


def normalize_text(t: str) -> str:
    t = _NONWORD.sub(" ", (t or "").lower())
    return _WS.sub(" ", t).strip()


def shingles(text: str, k: int = 3) -> Set[str]:
    toks = [w for w in normalize_text(text).split() if w not in _STOP]
    if len(toks) < k:
        return set(toks)
    return {" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1)}


def fingerprint(title: str, body: str = "") -> str:
    """Stable near-duplicate key: sorted content-word set of the title (order-insensitive)."""
    toks = sorted({w for w in normalize_text(title).split() if w not in _STOP and len(w) > 2})
    return hashlib.sha1(" ".join(toks).encode()).hexdigest()[:16]


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def tokens(text: str) -> Set[str]:
    return {w for w in normalize_text(text).split() if w not in _STOP and len(w) > 1}


def similarity(title_a: str, body_a: str, title_b: str, body_b: str) -> float:
    """Max of 3-shingle overlap (exact phrasing) and content-token overlap (paraphrases)."""
    if fingerprint(title_a) == fingerprint(title_b):
        return 1.0
    sa = shingles(title_a + " " + (body_a or "")[:300])
    sb = shingles(title_b + " " + (body_b or "")[:300])
    return max(jaccard(sa, sb), jaccard(tokens(title_a), tokens(title_b)))


def near_duplicate(title_a: str, body_a: str, title_b: str, body_b: str, threshold: float = 0.6) -> bool:
    return similarity(title_a, body_a, title_b, body_b) >= threshold


@dataclass
class SourceDocument:
    doc_id: str
    source: str                  # sec | alpaca_benzinga | alpaca | yahoo | fred | file
    doc_type: str                # filing | news | macro
    symbols: List[str]
    title: str
    published_at: str            # ISO UTC
    retrieved_at: str
    available_at: str
    primary_symbol: Optional[str] = None
    summary: str = ""
    content: str = ""            # full text if licensed; written to disk, not the DB
    url: str = ""
    provider_id: Optional[str] = None
    event_time: Optional[str] = None
    effective_date: Optional[str] = None
    source_quality: float = 0.5
    license_flags: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    quarantined: bool = False
    quarantine_reason: Optional[str] = None
    content_ref: Optional[str] = None

    @property
    def content_hash(self) -> str:
        return content_hash(self.source, self.title, self.summary, self.content[:4000])

    def validate(self, max_future_minutes: int = 5) -> "SourceDocument":
        """Quarantine malformed, future-dated, or implausibly timestamped data (design §6.3)."""
        why = None
        if not self.title.strip():
            why = "empty_title"
        elif not self.published_at or not self.available_at:
            why = "missing_timestamp"
        else:
            try:
                p = datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
                r = datetime.fromisoformat(self.retrieved_at.replace("Z", "+00:00"))
                if p > r + timedelta(minutes=max_future_minutes):
                    why = "future_dated"
                elif p < datetime(2000, 1, 1, tzinfo=timezone.utc):
                    why = "implausible_timestamp"
            except ValueError:
                why = "bad_timestamp"
        if why:
            self.quarantined, self.quarantine_reason = True, why
        return self

    def to_row(self) -> Dict[str, Any]:
        return {"doc_id": self.doc_id, "source": self.source, "provider_id": self.provider_id, "doc_type": self.doc_type,
                "symbols": self.symbols, "primary_symbol": self.primary_symbol or (self.symbols[0] if self.symbols else None),
                "title": self.title[:400], "summary": self.summary[:2000], "content_ref": self.content_ref, "url": self.url,
                "event_time": self.event_time, "published_at": self.published_at, "retrieved_at": self.retrieved_at,
                "available_at": self.available_at, "effective_date": self.effective_date, "content_hash": self.content_hash,
                "source_quality": self.source_quality, "license_flags": self.license_flags, "quarantined": self.quarantined,
                "quarantine_reason": self.quarantine_reason, "meta": self.meta}


class DocumentStore:
    """Writes bodies to disk and index rows to the SQLite store; returns ingest stats."""

    def __init__(self, store, docs_dir: Path):
        self.store = store
        self.dir = docs_dir

    def body_path(self, doc: SourceDocument) -> Path:
        d = self.dir / doc.available_at[:7]
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{doc.doc_id}.txt.gz"

    def read_body(self, content_ref: Optional[str]) -> str:
        if not content_ref:
            return ""
        p = Path(content_ref)
        if not p.exists():
            return ""
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return f.read()

    def ingest(self, docs: List[SourceDocument], run_id: Optional[str], universe: Set[str]) -> Dict[str, Any]:
        stats = {"new": 0, "existing": 0, "revised": 0, "quarantined": 0, "exact_dup": 0, "near_dup": 0, "ids": []}
        recent = self.store.documents((now_utc() - timedelta(days=4)).isoformat(), "9999", include_quarantined=False)
        seen_fp = {fingerprint(d["title"] or ""): d["doc_id"] for d in recent}
        for doc in docs:
            doc.symbols = [s for s in doc.symbols if s in universe]
            if not doc.symbols and doc.doc_type != "macro":
                continue
            doc.validate()
            if doc.quarantined:
                stats["quarantined"] += 1
                self.store.upsert_document(doc.to_row(), run_id)
                continue
            if self.store.doc_exists(doc.doc_id) is None:
                by_hash = self.store.doc_by_hash(doc.content_hash)
                if by_hash:
                    stats["exact_dup"] += 1
                    doc.meta["duplicate_of"] = by_hash["doc_id"]
                    continue
                fp = fingerprint(doc.title)
                if fp in seen_fp and seen_fp[fp] != doc.doc_id and doc.doc_type == "news":
                    stats["near_dup"] += 1
                    doc.meta["near_duplicate_of"] = seen_fp[fp]
                    doc.source_quality = min(doc.source_quality, 0.4)
                seen_fp.setdefault(fp, doc.doc_id)
            if doc.content:
                p = self.body_path(doc)
                if not p.exists():
                    with gzip.open(p, "wt", encoding="utf-8") as f:
                        f.write(doc.content)
                doc.content_ref = str(p)
            r = self.store.upsert_document(doc.to_row(), run_id)
            stats[r] += 1
            stats["ids"].append(doc.doc_id)
        return stats
