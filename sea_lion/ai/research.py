"""V2 multi-pass research orchestrator (design §7–§8).

extract (A, per document, cached by content) -> build canonical events (deterministic clustering,
B) -> route -> select candidates -> per candidate: audit (C) -> analyst (D) -> skeptic (E) ->
context (F) -> synthesize (G). Every pass is a strict-JSON call through the router; unsupported
evidence, contradictions, or analyst/skeptic disagreement shrink to neutral or abstain. A wall-clock
deadline stops low-priority work and marks the result degraded rather than blocking the decision."""
from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings
from ..data.documents import DocumentStore, normalize_text
from ..events.cluster import EventClusterer
from ..events.entities import EntityRegistry
from ..events.routing import route, select_candidates
from ..store import Store
from . import v2_prompts as P
from .router import ModelRouter
from .v2_schemas import (AnalystOutput, AuditOutput, ContextOutput, ExtractionOutput, HORIZON_KEYS, SkepticOutput,
                         SynthesisOutput)

log = logging.getLogger(__name__)


@dataclass
class ResearchResult:
    extraction: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    candidates: List[str] = field(default_factory=list)
    candidate_reasons: Dict[str, str] = field(default_factory=dict)
    forecasts: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # symbol -> packet
    degraded: List[str] = field(default_factory=list)
    deadline_hit: bool = False
    timings: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"extraction": self.extraction, "events": self.events, "candidates": self.candidates,
                "candidate_reasons": self.candidate_reasons, "forecasts": self.forecasts, "degraded": self.degraded,
                "deadline_hit": self.deadline_hit, "timings": self.timings}


def _claim_id(doc_id: str, text: str) -> str:
    return "clm_" + hashlib.sha1(f"{doc_id}|{normalize_text(text)}".encode()).hexdigest()[:14]


def _span_in(span: str, *texts: str) -> bool:
    s = normalize_text(span)
    if len(s) < 8:
        return False
    return any(s in normalize_text(t) for t in texts if t)


class ResearchPipeline:
    def __init__(self, cfg: Settings, store: Store, router: ModelRouter, docs: DocumentStore, registry: EntityRegistry,
                 run_id: str, as_of: str, cutoff: str, universe: List[str], deadline_sec: Optional[int] = None):
        self.cfg, self.store, self.router, self.docs, self.reg = cfg, store, router, docs, registry
        self.run_id, self.as_of, self.cutoff, self.universe = run_id, as_of, cutoff, universe
        self.rc = cfg.v2.research
        self.stage_budget = deadline_sec or self.rc.stage_deadline_sec
        self.per_stage = deadline_sec is None      # after-close job: each stage gets its own budget
        self.deadline = time.time() + self.stage_budget
        self.res = ResearchResult()
        self.clusterer = EventClusterer(store)

    def _time_left(self) -> float:
        return self.deadline - time.time()

    def _fail_reason(self) -> str:
        st = self.router.state
        if st.budget_status == "hard_stop":
            return "budget_exhausted"
        if st.provider_unavailable:
            return "provider_unavailable"
        return "invalid"

    # ------------------------------------------------------------------ A: extraction
    def extract(self, docs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Per document: validated claims with verbatim evidence spans. Returns doc_id -> meta+claims."""
        t0 = time.time()
        out: Dict[str, Dict[str, Any]] = {}
        docs = [d for d in docs if d["available_at"] <= self.cutoff]   # point-in-time guard
        # triage (design §8.3 priority queues): filings first, then by source quality; skip listicles,
        # near-duplicates and other low-quality items outright, and cap documents per symbol.
        docs = sorted(docs, key=lambda d: (0 if d["doc_type"] == "filing" else 1, -(d.get("source_quality") or 0)))
        per_sym: Dict[str, int] = {}
        todo, skipped_lowq, skipped_cap = [], 0, 0
        for d in docs:
            if d["doc_type"] != "filing" and (d.get("source_quality") or 0) < 0.4:
                skipped_lowq += 1
                continue
            sym = d.get("primary_symbol") or (d["symbols"][0] if d["symbols"] else "")
            if per_sym.get(sym, 0) >= self.rc.max_documents_per_symbol:
                skipped_cap += 1
                continue
            per_sym[sym] = per_sym.get(sym, 0) + 1
            todo.append(d)

        def one(d: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
            if self._time_left() <= 0:
                return d["doc_id"], None
            body = self.docs.read_body(d.get("content_ref"))[: self.cfg.v2.sources.news_max_content_chars]
            payload = dict(d, body=body)
            allowed = [s for s in d["symbols"] if s in self.universe] or self.universe
            obj = self.router.call_pass("cheap", "v2.extract", P.EXTRACT_SYSTEM, P.extract_user(payload, allowed), ExtractionOutput)
            if obj is None:
                return d["doc_id"], None
            claims = []
            for c in obj.claims:
                if not _span_in(c.evidence_span, d.get("title", ""), d.get("summary", ""), body):
                    continue
                syms = [s for s in c.symbols if s in self.universe] or [d.get("primary_symbol")]
                syms = [s for s in syms if s]
                if not syms:
                    continue
                claims.append({"claim_id": _claim_id(d["doc_id"], c.text), "doc_id": d["doc_id"], "symbol": syms[0],
                               "symbols": syms, "claim": c.text, "evidence_span": c.evidence_span, "quantity": c.quantity,
                               "claim_time": c.claim_time, "certainty": c.certainty, "event_type": c.event_type,
                               "is_forward_looking": c.is_forward_looking, "is_rumor": c.is_rumor})
            return d["doc_id"], {"relevance": obj.doc_relevance, "event_type": obj.doc_event_type, "sentiment": obj.doc_sentiment,
                                 "importance": obj.doc_importance, "one_line": obj.one_line, "claims": claims,
                                 "dropped_spans": len(obj.claims) - len(claims)}

        with ThreadPoolExecutor(max_workers=max(1, self.rc.concurrency)) as ex:
            for doc_id, meta in ex.map(one, todo):
                if meta is not None:
                    out[doc_id] = meta
        all_claims = [c for m in out.values() for c in m["claims"]]
        self.store.add_claims(all_claims, self.run_id)
        failed = len(todo) - len(out)
        if self.router.state.budget_status == "hard_stop" and "budget_exhausted" not in self.res.degraded:
            self.res.degraded.append("budget_exhausted")
        self.res.extraction = {"n_docs": len(todo), "n_extracted": len(out), "n_failed": failed, "n_claims": len(all_claims),
                               "dropped_unverifiable_spans": sum(m["dropped_spans"] for m in out.values()),
                               "skipped_low_quality": skipped_lowq, "skipped_per_symbol_cap": skipped_cap, "n_candidates_docs": len(docs)}
        if self._time_left() <= 0:
            self.res.deadline_hit = True
            self.res.degraded.append("extraction_deadline")
        self.res.timings["extract"] = round(time.time() - t0, 1)
        return out

    # ------------------------------------------------------------------ B + routing
    def build_events(self, docs: List[Dict[str, Any]], meta: Dict[str, Dict[str, Any]],
                     features: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        t0 = time.time()
        by_id = {d["doc_id"]: d for d in docs}
        events: Dict[str, Dict[str, Any]] = {}
        for doc_id, m in meta.items():
            d = by_id[doc_id]
            if m["relevance"] < 0.3 and d["doc_type"] != "filing":
                continue
            groups: Dict[str, List[Dict[str, Any]]] = {}
            for c in m["claims"]:
                for s in c["symbols"]:
                    groups.setdefault(s, []).append(c)
            if not groups and d.get("primary_symbol"):
                groups[d["primary_symbol"]] = []
            for sym, cls in groups.items():
                if sym not in self.universe:
                    continue
                etype = d["meta"].get("event_type") if d["doc_type"] == "filing" else m["event_type"]
                imp = max(float(d["meta"].get("importance", 0) or 0), m["importance"]) if d["doc_type"] == "filing" else m["importance"]
                row, outcome = self.clusterer.assign(sym, etype or "other", d.get("title", ""), m["one_line"], d["available_at"],
                                                     doc_id, [c["claim_id"] for c in cls], imp, m["sentiment"],
                                                     float(d.get("source_quality") or 0.5), self.run_id,
                                                     related_symbols=[s for s in d["symbols"] if s != sym][:6],
                                                     event_time=d.get("event_time"))
                row["_is_filing"] = row.get("_is_filing") or d["doc_type"] == "filing"
                row["_rumor"] = bool(cls) and all(c["is_rumor"] for c in cls)
                events[row["event_id"]] = row
        # routing needs corroboration: another actionable non-analyst event for the symbol, or unusual volume
        by_sym: Dict[str, List[Dict[str, Any]]] = {}
        for e in events.values():
            by_sym.setdefault(e["primary_symbol"], []).append(e)
        for sym, evs in by_sym.items():
            vol_ratio = float((features.get(sym) or {}).get("volume_ratio") or 0.0)
            non_analyst = [e for e in evs if e["event_type"] not in ("analyst", "macro")]
            for e in evs:
                corroborated = vol_ratio >= 1.5 or any(o is not e for o in non_analyst)
                cls, actionable, reason = route(e, is_filing=bool(e.get("_is_filing")), is_rumor=bool(e.get("_rumor")),
                                                corroborated=corroborated, macro_gate=self.rc.macro_gate_importance,
                                                analyst_requires_corroboration=self.rc.analyst_requires_corroboration,
                                                has_evidence=bool(e.get("document_ids")))
                e["routing_class"], e["actionable"], e["routing_reason"] = cls, actionable, reason
                e["status"] = "routed"
                self.store.upsert_event({k: v for k, v in e.items() if not k.startswith("_")} | {"run_id": self.run_id})
        self.res.events = [{k: v for k, v in e.items() if not k.startswith("_")} for e in events.values()]
        self.res.timings["events"] = round(time.time() - t0, 1)
        return self.res.events

    def choose_candidates(self, events: List[Dict[str, Any]], quant_rank: List[str]) -> List[str]:
        by_sym: Dict[str, List[Dict[str, Any]]] = {}
        for e in events:
            by_sym.setdefault(e["primary_symbol"], []).append(e)
        cands, why = select_candidates(by_sym, quant_rank, self.rc.main_candidates, self.rc.reserve_for_filings,
                                       self.rc.material_importance)
        self.res.candidates, self.res.candidate_reasons = cands, why
        return cands

    # ------------------------------------------------------------------ C..G per candidate
    def research_candidate(self, sym: str, events: List[Dict[str, Any]], snapshot: Dict[str, Any], regime: Dict[str, Any],
                           macro: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.time()
        evs = [e for e in events if e["primary_symbol"] == sym and e.get("actionable")]
        packet: Dict[str, Any] = {"symbol": sym, "events": [e["event_id"] for e in evs], "abstain": True, "abstain_reason": "",
                                  "supported_claim_ids": [], "audits": {}, "analyst": None, "skeptic": None, "context": None,
                                  "synth": None, "evidence_quality": 0.0, "disagreement": 0.0, "freshness": 0.0}
        if not evs:
            packet["abstain_reason"] = "no_actionable_events"
            return packet
        claims = self.store.claims_for_docs([d for e in evs for d in e["document_ids"]])
        claims = [c for c in claims if c["symbol"] == sym or sym in (c.get("symbol") or "")]
        by_event: Dict[str, List[Dict[str, Any]]] = {}
        cid_set = {c["claim_id"] for c in claims}
        for e in evs:
            by_event[e["event_id"]] = [c for c in claims if c["claim_id"] in set(e["claim_ids"]) & cid_set]
        supported: List[Dict[str, Any]] = []
        eq_vals, contra = [], []
        eq_weights_list: List[Tuple[float, int]] = []
        for e in evs:
            ecl = by_event[e["event_id"]]
            if not ecl:
                continue
            material = float(e.get("importance") or 0) >= self.rc.material_importance
            if material or not self.rc.audit_material_only:
                excerpt = ""
                for did in e["document_ids"]:
                    d = self.store.doc_exists(did)
                    if d and d["doc_type"] == "filing":
                        excerpt = self.docs.read_body(d.get("content_ref"))[:3000]
                        break
                a = self.router.call_pass("main", "v2.audit", P.AUDIT_SYSTEM,
                                          P.audit_user(e, [{k: c[k] for k in ("claim_id", "claim", "evidence_span", "certainty", "event_type")}
                                                           | {"source": self.store.doc_exists(c["doc_id"])["source"] if self.store.doc_exists(c["doc_id"]) else "?"} for c in ecl], excerpt),
                                          AuditOutput, lambda o, ids={c["claim_id"] for c in ecl}: None if set(o.supported_claim_ids) <= ids and set(o.unsupported_claim_ids) <= ids else "unknown claim ids in audit")
                if a is None:
                    packet["audits"][e["event_id"]] = {"failed": True, "reason": self._fail_reason()}
                    continue
                packet["audits"][e["event_id"]] = a.model_dump()
                supported += [c for c in ecl if c["claim_id"] in set(a.supported_claim_ids)]
                eq_vals.append(a.evidence_quality)
                eq_weights_list.append((a.evidence_quality, len(set(a.supported_claim_ids))))
                contra.append(a.contradiction_level)
                self.store.upsert_event(dict(e, contradiction_level=a.contradiction_level, status="verified" if a.supported_claim_ids else "unsupported",
                                             audit={"evidence_quality": a.evidence_quality, "unsupported": a.unsupported_claim_ids,
                                                    "missing_primary": a.missing_primary_evidence}, run_id=self.run_id))
            else:
                supported += ecl
                eq_vals.append(float(e.get("source_quality") or 0.5))
                eq_weights_list.append((float(e.get("source_quality") or 0.5), len(ecl)))
                contra.append(0.0)
        packet["supported_claim_ids"] = [c["claim_id"] for c in supported]
        # evidence quality = supported-claim-weighted mean of audit quality (a verified 8-K with many
        # supported claims should not be dragged to the floor by one thin secondary story)
        if eq_weights := [w for w in eq_weights_list if w[1] > 0]:
            packet["evidence_quality"] = round(sum(q * n for q, n in eq_weights) / sum(n for _, n in eq_weights), 3)
        else:
            packet["evidence_quality"] = round(min(eq_vals) if eq_vals else 0.0, 3)
        if not supported:
            packet["abstain_reason"] = "no_supported_claims" + ("_budget_exhausted" if self.router.state.budget_status == "hard_stop" else "")
            return packet
        if (max(contra) if contra else 0.0) >= 0.8:
            packet["abstain_reason"] = "high_contradiction"
            return packet
        newest = max(e["available_at"] for e in evs)
        # freshness is relative to the decision session (end of as_of), not to wall-clock time, so
        # replays and after-close research agree; a 5-day-old event contributes nothing.
        ref = datetime.fromisoformat(self.as_of + "T23:59:59+00:00")
        age_days = max(0.0, (ref - datetime.fromisoformat(newest.replace("Z", "+00:00"))).total_seconds() / 86400)
        packet["freshness"] = round(max(0.0, 1.0 - age_days / 5.0), 3)
        history = [{"date": e["available_at"][:10], "type": e["event_type"], "title": e["title"]} for e in
                   self.store.events((datetime.fromisoformat(self.cutoff.replace("Z", "+00:00")) - timedelta(days=30)).isoformat(), self.cutoff)
                   if e["primary_symbol"] == sym][-8:]
        ev_summary = [{k: e.get(k) for k in ("event_id", "event_type", "title", "available_at", "novelty", "source_quality", "routing_class")} for e in evs]
        sup = [{k: c[k] for k in ("claim_id", "claim", "evidence_span", "certainty", "event_type", "quantity", "claim_time")} for c in supported][: self.rc.max_claims_per_event * 2]
        if self._time_left() <= 0:
            packet["abstain_reason"] = "deadline"
            self.res.deadline_hit = True
            return packet
        packet_user = P.analyst_user(sym, snapshot, ev_summary, sup, history)
        allowed_ids = {c["claim_id"] for c in supported}
        analyst = self.router.call_pass("main", "v2.analyst", P.ANALYST_SYSTEM, packet_user, AnalystOutput,
                                        lambda o: None if o.symbol == sym and set(o.evidence_ids) <= allowed_ids else "analyst: bad symbol or evidence ids")
        if analyst is None:
            packet["abstain_reason"] = "analyst_" + self._fail_reason()
            return packet
        packet["analyst"] = analyst.model_dump()
        skeptic = None
        if self.rc.skeptic_enabled and self._time_left() > 0:
            skeptic = self.router.call_pass("main", "v2.skeptic", P.SKEPTIC_SYSTEM, P.skeptic_user(sym, packet_user, packet["analyst"]), SkepticOutput)
            packet["skeptic"] = skeptic.model_dump() if skeptic else None
        context = None
        if self.rc.context_enabled and self._time_left() > 0:
            context = self.router.call_pass("main", "v2.context", P.CONTEXT_SYSTEM,
                                            P.context_user(sym, self.reg.sector(sym), regime, macro, ev_summary, self.universe), ContextOutput,
                                            lambda o: None if all(s.symbol in self.universe for s in o.second_order) else "context: unknown symbol")
            packet["context"] = context.model_dump() if context else None
        # deterministic disagreement measure (design §8.2: not model voting)
        if skeptic:
            gaps = [abs(analyst.horizons[k].p_positive_excess_return - skeptic.p_positive_excess_return[k]) for k in HORIZON_KEYS]
            packet["disagreement"] = round(sum(gaps) / len(gaps), 3)
        if self._time_left() <= 0:
            packet["abstain_reason"] = "deadline_before_synthesis"
            self.res.deadline_hit = True
            return packet
        synth = self.router.call_pass("main", "v2.synth", P.SYNTH_SYSTEM,
                                      P.synth_user(sym, {k: packet["audits"][k] for k in packet["audits"]}, packet["analyst"], packet["skeptic"] or {},
                                                   packet["context"], sorted(allowed_ids)), SynthesisOutput,
                                      lambda o: None if o.symbol == sym and set(o.evidence_ids) <= allowed_ids else "synth: bad symbol or evidence ids")
        if synth is None:
            packet["abstain_reason"] = "synthesis_" + self._fail_reason()
            return packet
        packet["synth"] = synth.model_dump()
        packet["abstain"] = bool(synth.abstain) or (skeptic is not None and skeptic.recommend_abstain and packet["disagreement"] > self.rc.disagreement_abstain / 2)
        if packet["disagreement"] > self.rc.disagreement_abstain:
            packet["abstain"], packet["abstain_reason"] = True, f"disagreement_{packet['disagreement']:.2f}"
        elif packet["abstain"]:
            packet["abstain_reason"] = synth.abstain_reason or "model_abstained"
        if not synth.evidence_ids and not packet["abstain"]:
            packet["abstain"], packet["abstain_reason"] = True, "no_evidence_cited"
        packet["evidence_quality"] = round(0.5 * (packet["evidence_quality"] + synth.evidence_quality), 3)   # audit-weighted and synthesizer views
        packet["seconds"] = round(time.time() - t0, 1)
        return packet

    def run_candidates(self, cands: List[str], events: List[Dict[str, Any]], snapshots: Dict[str, Dict[str, Any]],
                       regime: Dict[str, Any], macro: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        t0 = time.time()
        if self.per_stage:
            self.deadline = time.time() + self.stage_budget     # fresh budget for the candidate passes
        workers = max(1, min(4, self.rc.concurrency))     # passes within a candidate are sequential; candidates run in parallel
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sym, packet in zip(cands, ex.map(lambda s: self.research_candidate(s, events, snapshots.get(s, {}), regime, macro), cands)):
                self.res.forecasts[sym] = packet
        self.res.timings["candidates"] = round(time.time() - t0, 1)
        if self.res.deadline_hit:
            self.res.degraded.append("candidate_deadline")
        if self.router.state.budget_status == "hard_stop" and "budget_exhausted" not in self.res.degraded:
            self.res.degraded.append("budget_exhausted")
        return self.res.forecasts
