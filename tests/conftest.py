"""Shared fixtures: synthetic bars, fake LLM provider, in-memory config."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Dict, List

import numpy as np
import pandas as pd
import pytest

from sea_lion import config as C
from sea_lion.ai.providers import ModelResponse
from sea_lion.data.base import BAR_COLUMNS, Event
from sea_lion.store import Store

SYMS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "V", "UNH", "LLY", "XOM", "CVX", "COST", "PG",
        "CAT", "HON", "LIN", "NEE", "PLD", "SPY", "QQQ", "XLK", "XLF", "GLD", "TLT"]


def synthetic_bars(symbols: List[str] = SYMS, n_days: int = 320, end: date = date(2026, 8, 28), seed: int = 7,
                   drift: Dict[str, float] | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(end=end, periods=n_days)
    rows = []
    for i, s in enumerate(symbols):
        mu = (drift or {}).get(s, 0.0003 + 0.0002 * (i % 5))
        sig = 0.01 + 0.004 * (i % 4)
        r = rng.normal(mu, sig, n_days)
        px = 100 * np.exp(np.cumsum(r))
        vol = rng.integers(2_000_000, 20_000_000, n_days).astype(float)
        for d, p, v, rr in zip(days, px, vol, r):
            o = p * (1 - rr / 2)
            rows.append({"symbol": s, "date": d.date().isoformat(), "open": o, "high": max(o, p) * 1.005,
                         "low": min(o, p) * 0.995, "close": p, "adj_close": p, "volume": v})
    return pd.DataFrame(rows)[BAR_COLUMNS]


@pytest.fixture
def cfg(tmp_path) -> C.Settings:
    c = C.load(mode="sim", overrides={"data": {"events_provider": "none"}, "ai": {"enabled": True},
                                       "broker": {"provider": "sim", "initial_cash": 2000.0}})
    c.universe = C.UniverseCfg(benchmark="SPY", symbols={s: SECTORS.get(s, "Other") for s in SYMS})
    c.runtime_dir = str(tmp_path / "rt")
    return c


SECTORS = {"AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology", "AMZN": "Consumer Discretionary",
           "GOOGL": "Communication Services", "META": "Communication Services", "JPM": "Financials", "V": "Financials",
           "UNH": "Health Care", "LLY": "Health Care", "XOM": "Energy", "CVX": "Energy", "COST": "Consumer Staples",
           "PG": "Consumer Staples", "CAT": "Industrials", "HON": "Industrials", "LIN": "Materials", "NEE": "Utilities",
           "PLD": "Real Estate", "SPY": "Broad", "QQQ": "Broad", "XLK": "Technology", "XLF": "Financials",
           "GLD": "Commodities", "TLT": "Bonds"}


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "t.db")


class FakeMarketData:
    name = "fake"

    def __init__(self, bars: pd.DataFrame):
        self.bars = bars
        self.calls = 0

    def fetch_bars(self, symbols, start, end):
        self.calls += 1
        b = self.bars[self.bars["symbol"].isin(symbols)]
        return b[(b["date"] >= start.isoformat()) & (b["date"] <= end.isoformat())].reset_index(drop=True)

    def latest_prices(self, symbols):
        last = self.bars.sort_values("date").groupby("symbol").last()
        return {s: float(last.loc[s, "close"]) for s in symbols if s in last.index}


class FakeEvents:
    name = "fake_events"

    def __init__(self, events: List[Event]):
        self.events = events

    def fetch_events(self, symbols, since, max_per_symbol):
        return [e for e in self.events if e.symbol in symbols]


class FakeProvider:
    """Scripted LLM: returns queued texts in order, or a generated valid answer."""
    name = "fake"

    def __init__(self, scripted: List[str] | None = None, impact: float = 0.5, confidence: float = 0.9,
                 fail: bool = False, p_analyst: float = 0.75, p_skeptic: float = 0.7, skeptic_abstain: bool = False,
                 synth_abstain: bool = False, synth_no_evidence: bool = False):
        self.scripted = list(scripted or [])
        self.impact, self.confidence, self.fail = impact, confidence, fail
        self.p_analyst, self.p_skeptic, self.skeptic_abstain = p_analyst, p_skeptic, skeptic_abstain
        self.synth_abstain, self.synth_no_evidence = synth_abstain, synth_no_evidence
        self.calls: List[Dict] = []

    def healthy(self):
        return not self.fail

    def complete_json(self, system, user, schema, max_tokens, temperature=0.0):
        from sea_lion.ai.providers import ProviderError
        self.calls.append({"system": system, "user": user})
        if self.fail:
            raise ProviderError("fake provider down")
        if self.scripted:
            text = self.scripted.pop(0)
        else:
            text = self._auto(user, schema)
        return ModelResponse(text=text, input_tokens=100, output_tokens=50, latency_ms=5, model="fake")

    def _auto(self, user: str, schema: dict) -> str:
        props = schema.get("properties", {})
        # ---- V2 passes -------------------------------------------------------
        if "claims" in props and "doc_relevance" in props:
            block = user.split("<<<BEGIN_UNTRUSTED_DATA>>>")[1].split("<<<END_UNTRUSTED_DATA>>>")[0]
            d = json.loads(block)
            syms = d.get("provider_symbols") or []
            span = (d.get("summary") or d.get("title") or "")[:120]
            title = d.get("title", "")
            etype = "earnings" if "beats" in title.lower() or "earnings" in title.lower() else ("macro" if "fed" in title.lower() else "product")
            imp = 0.8 if etype == "earnings" else (0.3 if etype == "macro" else 0.55)
            claims = [{"text": f"{title} (claim)", "evidence_span": span, "symbols": syms[:1], "event_type": etype, "quantity": None,
                       "claim_time": d.get("published_at", "")[:10], "certainty": 0.8, "is_forward_looking": False,
                       "is_rumor": "rumor" in title.lower()}]
            if "INVENTED_SPAN" in user:
                claims.append({"text": "fabricated", "evidence_span": "this text does not exist anywhere in the document", "symbols": syms[:1],
                               "event_type": etype, "certainty": 0.9, "is_forward_looking": False, "is_rumor": False})
            return json.dumps({"doc_relevance": 0.9, "doc_event_type": etype, "doc_sentiment": 0.5, "doc_importance": imp,
                               "one_line": title[:100], "claims": claims})
        if "supported_claim_ids" in props:
            block = user.split("<<<BEGIN_UNTRUSTED_DATA>>>")[1].split("<<<END_UNTRUSTED_DATA>>>")[0]
            d = json.loads(block)
            ids = [c["claim_id"] for c in d["claims"]]
            return json.dumps({"supported_claim_ids": ids, "unsupported_claim_ids": [], "contradictions": [], "missing_primary_evidence": False,
                               "contradiction_level": 0.05, "evidence_quality": 0.8, "notes": "ok"})
        if "bull_mechanisms" in props:
            block = user.split("<<<BEGIN_UNTRUSTED_DATA>>>")[1].split("<<<END_UNTRUSTED_DATA>>>")[0]
            d = json.loads(block)
            ids = [c["claim_id"] for c in d["supported_claims"]]
            h = {k: {"expected_excess_return_bps": 80, "p_positive_excess_return": self.p_analyst} for k in ("5d", "10d", "20d")}
            return json.dumps({"symbol": d["ticker"], "bull_mechanisms": ["b"], "bear_mechanisms": ["r"], "affected_metrics": ["revenue"], "horizons": h,
                               "catalyst_half_life_days": 6, "invalidation_conditions": ["x"], "market_likely_priced_in": False,
                               "evidence_ids": ids[:2], "notes": "n"})
        if "alternative_explanations" in props:
            return json.dumps({"alternative_explanations": ["a"], "market_pricing_argument": "m", "failure_modes": ["f"], "evidence_gaps": [],
                               "p_positive_excess_return": {k: self.p_skeptic for k in ("5d", "10d", "20d")}, "recommend_abstain": self.skeptic_abstain})
        if "sector_channel" in props:
            return json.dumps({"sector_channel": "s", "second_order": [], "adjustment_bps": {"5d": 5, "10d": 5, "20d": 5}, "macro_flags": []})
        if "abstain" in props and "rationale" in props:
            block = user.split("<<<BEGIN_UNTRUSTED_DATA>>>")[1].split("<<<END_UNTRUSTED_DATA>>>")[0]
            d = json.loads(block)
            ids = d["supported_claim_ids"]
            p = (self.p_analyst + self.p_skeptic) / 2
            h = {k: {"expected_excess_return_bps": 60, "p_positive_excess_return": p} for k in ("5d", "10d", "20d")}
            return json.dumps({"symbol": d["ticker"], "horizons": h, "evidence_quality": 0.8, "model_disagreement": abs(self.p_analyst - self.p_skeptic),
                               "catalyst_half_life_days": 6, "invalidation_conditions": [], "risk_flags": ["single_source"],
                               "evidence_ids": ids[:1] if not self.synth_no_evidence else [], "abstain": self.synth_abstain, "abstain_reason": "", "rationale": "r"})
        # ---- V1 passes -------------------------------------------------------
        if "facts" in props:
            block = user.split("<<<BEGIN_UNTRUSTED_NEWS_DATA>>>")[1].split("<<<END_UNTRUSTED_NEWS_DATA>>>")[0]
            items = json.loads(block)
            facts = [{"event_id": it["event_id"], "ticker": it["ticker"], "event_type": "earnings", "relevant": True,
                      "sentiment": 0.6, "importance": 0.8, "confidence": 0.9, "duplicate_of": None,
                      "one_line": "beat"} for it in items]
            return json.dumps({"facts": facts})
        sym = user.split("Ticker: ")[1].split("\n")[0].strip()
        block = user.split("<<<BEGIN_UNTRUSTED_EVENT_FACTS>>>")[1].split("<<<END_UNTRUSTED_EVENT_FACTS>>>")[0]
        ids = [f["event_id"] for f in json.loads(block)]
        return json.dumps({"symbol": sym, "bull_case": "b", "bear_case": "r", "horizon_days": 5, "impact": self.impact,
                           "confidence": self.confidence, "risk_flags": ["single_source"], "evidence_ids": ids[:1]})


def make_events(symbols: List[str], when: date) -> List[Event]:
    ts = datetime.combine(when, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    return [Event(symbol=s, published_at=ts, title=f"{s} beats estimates", summary="Revenue above consensus.",
                  source="test") for s in symbols]



class FakeDocs:
    """V2 document source for tests."""
    name = "fake_docs"

    def __init__(self, docs):
        self.docs = list(docs)
        self.calls = 0

    def fetch_documents(self, symbols, since, limit):
        self.calls += 1
        return [d for d in self.docs if any(s in symbols for s in d.symbols)]


def make_docs(symbols, when: date, titles=None, source="alpaca_benzinga", quality=0.7, body=True):
    from sea_lion.data.documents import SourceDocument
    out = []
    ts = datetime.combine(when, datetime.min.time(), tzinfo=timezone.utc).replace(hour=20).isoformat()
    for i, s in enumerate(symbols):
        title = (titles or {}).get(s, f"{s} beats estimates and raises guidance")
        out.append(SourceDocument(doc_id=f"doc_{s}_{when.isoformat()}_{i}", source=source, doc_type="news", symbols=[s], primary_symbol=s,
                                  title=title, summary=f"{s} reported revenue above consensus and raised its outlook.",
                                  content=(f"{s} reported quarterly revenue above consensus. Management raised full-year guidance." if body else ""),
                                  published_at=ts, retrieved_at=ts, available_at=ts, source_quality=quality, license_flags="test"))
    return out
