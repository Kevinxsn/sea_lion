"""Event routing policy (design §7.2, §7.4): which canonical events may reach the main tier and
which are stored but non-actionable. Deterministic; runs before the expensive passes."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

FILING_MATERIAL = {"earnings", "guidance", "non_reliance_restatement", "bankruptcy", "acquisition_disposition",
                   "control_change", "material_agreement", "cybersecurity_incident", "impairment", "restructuring"}
FILING_PERIODIC = {"quarterly_report", "annual_report", "current_report", "management", "reg_fd", "other_event",
                   "debt_obligation", "debt_acceleration", "auditor_change", "listing_notice"}
CORPORATE = {"m_and_a", "legal", "regulatory", "management", "capital_return", "product", "guidance", "earnings"}
CLASS_PRIORITY = {"filing_material": 1, "filing_periodic": 2, "corporate_event": 3, "company_news": 4, "analyst": 5,
                  "macro": 6, "rumor": 8, "duplicate": 9, "low_quality": 9}


def classify(event: Dict[str, Any], is_filing: bool) -> str:
    et = event["event_type"]
    if is_filing:
        return "filing_material" if et in FILING_MATERIAL else "filing_periodic"
    if et == "macro":
        return "macro"
    if et == "analyst":
        return "analyst"
    if et in CORPORATE:
        return "corporate_event"
    return "company_news"


def route(event: Dict[str, Any], *, is_filing: bool, is_rumor: bool, corroborated: bool, macro_gate: float,
          analyst_requires_corroboration: bool, quality_floor: float = 0.4, novelty_floor: float = 0.15,
          has_evidence: bool = True) -> Tuple[str, bool, str]:
    """Return (routing_class, actionable, reason)."""
    cls = classify(event, is_filing)
    imp = float(event.get("importance") or 0.0)
    q = float(event.get("source_quality") or 0.0)
    nov = float(event.get("novelty") if event.get("novelty") is not None else 1.0)
    if not has_evidence:
        return cls, False, "no_evidence_document"
    if is_rumor:
        return "rumor", False, "rumor_or_speculation"
    if q < quality_floor:
        return "low_quality", False, f"source_quality_{q:.2f}<{quality_floor}"
    if nov < novelty_floor and event.get("status") != "updated":
        return "duplicate", False, f"novelty_{nov:.2f}<{novelty_floor}"
    if cls == "macro":
        if imp < macro_gate:
            return cls, False, f"macro_importance_{imp:.2f}<{macro_gate}"
        return cls, True, "macro_high_importance"
    if cls == "analyst" and analyst_requires_corroboration and not corroborated:
        return cls, False, "analyst_uncorroborated"
    if cls == "filing_periodic" and imp < 0.3:
        return cls, False, "periodic_low_novelty"
    return cls, True, "ok"


def select_candidates(events_by_symbol: Dict[str, List[Dict[str, Any]]], quant_rank: List[str], main_candidates: int,
                      reserve_for_filings: int, material_importance: float) -> Tuple[List[str], Dict[str, str]]:
    """Choose symbols for the main tier: reserved slots for filing events first, then quant-ranked
    symbols with actionable events, then any symbol with a highly important event."""
    why: Dict[str, str] = {}
    chosen: List[str] = []

    def add(sym: str, reason: str) -> None:
        if sym not in chosen and len(chosen) < main_candidates:
            chosen.append(sym)
            why[sym] = reason

    filing_syms = [s for s, evs in events_by_symbol.items()
                   if any(e.get("actionable") and e.get("routing_class") == "filing_material" for e in evs)]
    filing_syms.sort(key=lambda s: -max(float(e.get("importance") or 0) for e in events_by_symbol[s]))
    for s in filing_syms[:reserve_for_filings]:
        add(s, "material_filing")
    for s in quant_rank:
        if s in events_by_symbol and any(e.get("actionable") for e in events_by_symbol[s]):
            add(s, "quant_rank_with_event")
    important = sorted([s for s, evs in events_by_symbol.items()
                        if any(e.get("actionable") and float(e.get("importance") or 0) >= material_importance for e in evs)],
                       key=lambda s: -max(float(e.get("importance") or 0) for e in events_by_symbol[s]))
    for s in important:
        add(s, "high_importance_event")
    return chosen, why
