"""V2 strict JSON contracts for each research pass (design §8.1, §10). Prompts never reveal
production thresholds; the model reports estimates and deterministic code applies gates."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, get_args

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

V2_SCHEMA_VERSION = "v2.1"
ClaimEventType = Literal["earnings", "guidance", "m_and_a", "product", "regulatory", "legal", "macro", "analyst",
                         "management", "capital_return", "litigation", "cybersecurity_incident", "restructuring",
                         "material_agreement", "other"]
HORIZON_KEYS = ("5d", "10d", "20d")


class HorizonForecast(BaseModel):
    expected_excess_return_bps: int = Field(0, ge=-2000, le=2000)
    p_positive_excess_return: float = Field(0.5, ge=0.0, le=1.0)


def _horizons_ok(h: Dict[str, HorizonForecast]) -> Dict[str, HorizonForecast]:
    for k in HORIZON_KEYS:
        h.setdefault(k, HorizonForecast())
    return {k: h[k] for k in HORIZON_KEYS}


_EVENT_TYPES = set(get_args(ClaimEventType))
_EVENT_SYNONYMS = {"merger": "m_and_a", "acquisition": "m_and_a", "m&a": "m_and_a", "deal": "m_and_a", "lawsuit": "litigation",
                   "legal_action": "litigation", "executive": "management", "leadership": "management", "dividend": "capital_return",
                   "buyback": "capital_return", "share_repurchase": "capital_return", "cyber": "cybersecurity_incident",
                   "layoffs": "restructuring", "contract": "material_agreement", "partnership": "material_agreement",
                   "macroeconomic": "macro", "economy": "macro", "analyst_rating": "analyst", "price_target": "analyst",
                   "earnings_report": "earnings", "results": "earnings", "outlook": "guidance", "forecast": "guidance"}


def _coerce_event_type(v: Any) -> str:
    t = str(v or "other").strip().lower().replace(" ", "_").replace("-", "_")
    if t in _EVENT_TYPES:
        return t
    return _EVENT_SYNONYMS.get(t, "other")


def _trunc(v: Any, n: int) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s[:n] if s else None


# ---- Pass A: extractor ------------------------------------------------------
class Claim(BaseModel):
    """Lenient on the model's surface (key aliases, truncation, enum coercion); strict on what matters
    downstream (the evidence span is verified verbatim by code, tickers are checked against the universe)."""
    text: str = Field(..., max_length=400, validation_alias=AliasChoices("text", "claim", "statement", "claim_text", "fact"))
    evidence_span: str = Field(..., max_length=400, validation_alias=AliasChoices("evidence_span", "evidence", "span", "quote"))
    symbols: List[str] = Field(default_factory=list, max_length=8, validation_alias=AliasChoices("symbols", "tickers", "ticker", "symbol"))
    event_type: str = "other"
    quantity: Optional[str] = None
    claim_time: Optional[str] = None
    certainty: float = Field(0.5, ge=0.0, le=1.0)
    is_forward_looking: bool = False
    is_rumor: bool = False

    @model_validator(mode="before")
    @classmethod
    def _pre(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        for k in ("text", "claim", "statement", "claim_text", "fact"):
            if k in d and d[k] is not None:
                d[k] = _trunc(d[k], 400)
        for k in ("evidence_span", "evidence", "span", "quote"):
            if k in d and d[k] is not None:
                d[k] = _trunc(d[k], 400)
        if "quantity" in d:
            d["quantity"] = _trunc(d["quantity"], 80)
        if "claim_time" in d:
            d["claim_time"] = _trunc(d["claim_time"], 80)
        if "event_type" in d:
            d["event_type"] = _coerce_event_type(d["event_type"])
        for k in ("symbols", "tickers", "ticker", "symbol"):
            if k in d and isinstance(d[k], str):
                d[k] = [d[k]]
        if "certainty" in d:
            try:
                d["certainty"] = min(1.0, max(0.0, float(d["certainty"])))
            except (TypeError, ValueError):
                d["certainty"] = 0.5
        return d

    @field_validator("symbols")
    @classmethod
    def _up(cls, v: List[str]) -> List[str]:
        return [str(s).strip().upper() for s in v if str(s).strip()][:8]


class ExtractionOutput(BaseModel):
    doc_relevance: float = Field(0.0, ge=0.0, le=1.0)
    doc_event_type: str = "other"
    doc_sentiment: float = Field(0.0, ge=-1.0, le=1.0)
    doc_importance: float = Field(0.0, ge=0.0, le=1.0)
    one_line: str = Field("", max_length=300)
    claims: List[Claim] = Field(default_factory=list, max_length=12)

    @model_validator(mode="before")
    @classmethod
    def _pre(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        d["doc_event_type"] = _coerce_event_type(d.get("doc_event_type"))
        d["one_line"] = _trunc(d.get("one_line"), 300) or ""
        for k in ("doc_relevance", "doc_importance"):
            try:
                d[k] = min(1.0, max(0.0, float(d.get(k, 0.0))))
            except (TypeError, ValueError):
                d[k] = 0.0
        try:
            d["doc_sentiment"] = min(1.0, max(-1.0, float(d.get("doc_sentiment", 0.0))))
        except (TypeError, ValueError):
            d["doc_sentiment"] = 0.0
        claims = d.get("claims")
        if isinstance(claims, list):
            d["claims"] = [c for c in claims if isinstance(c, dict)][:12]
        else:
            d["claims"] = []
        return d


# ---- Pass C: evidence auditor ----------------------------------------------
class Contradiction(BaseModel):
    claim_ids: List[str] = Field(default_factory=list, max_length=6)
    description: str = Field("", max_length=300)


class AuditOutput(BaseModel):
    supported_claim_ids: List[str] = Field(default_factory=list, max_length=40)
    unsupported_claim_ids: List[str] = Field(default_factory=list, max_length=40)
    contradictions: List[Contradiction] = Field(default_factory=list, max_length=10)
    missing_primary_evidence: bool = False
    contradiction_level: float = Field(0.0, ge=0.0, le=1.0)
    evidence_quality: float = Field(0.5, ge=0.0, le=1.0)
    notes: str = Field("", max_length=400)


# ---- Pass D: company analyst -----------------------------------------------
def _trunc_list(v: Any, n: int, each: int) -> List[str]:
    if not isinstance(v, list):
        return []
    return [str(x)[:each] for x in v if x is not None][:n]


class AnalystOutput(BaseModel):
    symbol: str
    bull_mechanisms: List[str] = Field(default_factory=list, max_length=4)
    bear_mechanisms: List[str] = Field(default_factory=list, max_length=4)
    affected_metrics: List[str] = Field(default_factory=list, max_length=6)
    horizons: Dict[str, HorizonForecast] = Field(default_factory=dict)
    catalyst_half_life_days: int = Field(5, ge=1, le=60)
    invalidation_conditions: List[str] = Field(default_factory=list, max_length=5)
    market_likely_priced_in: bool = False
    evidence_ids: List[str] = Field(default_factory=list, max_length=20)
    notes: str = Field("", max_length=600)

    @model_validator(mode="before")
    @classmethod
    def _pre(cls, d: Any) -> Any:
        if not isinstance(d, dict):
            return d
        d = dict(d)
        for k, n, each in (("bull_mechanisms", 4, 300), ("bear_mechanisms", 4, 300), ("affected_metrics", 6, 80), ("invalidation_conditions", 5, 200), ("evidence_ids", 20, 60)):
            d[k] = _trunc_list(d.get(k), n, each)
        d["notes"] = _trunc(d.get("notes"), 600) or ""
        return d

    @field_validator("symbol")
    @classmethod
    def _up(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("horizons")
    @classmethod
    def _h(cls, v):
        return _horizons_ok(dict(v))


# ---- Pass E: skeptical reviewer --------------------------------------------
class SkepticOutput(BaseModel):
    alternative_explanations: List[str] = Field(default_factory=list, max_length=4)
    market_pricing_argument: str = Field("", max_length=400)
    failure_modes: List[str] = Field(default_factory=list, max_length=5)
    evidence_gaps: List[str] = Field(default_factory=list, max_length=5)
    p_positive_excess_return: Dict[str, float] = Field(default_factory=dict)
    recommend_abstain: bool = False

    @model_validator(mode="before")
    @classmethod
    def _pre(cls, d: Any) -> Any:
        if not isinstance(d, dict):
            return d
        d = dict(d)
        for k, n, each in (("alternative_explanations", 4, 300), ("failure_modes", 5, 300), ("evidence_gaps", 5, 300)):
            d[k] = _trunc_list(d.get(k), n, each)
        d["market_pricing_argument"] = _trunc(d.get("market_pricing_argument"), 400) or ""
        if not isinstance(d.get("p_positive_excess_return"), dict):
            d["p_positive_excess_return"] = {}
        return d

    @field_validator("p_positive_excess_return")
    @classmethod
    def _p(cls, v: Dict[str, float]) -> Dict[str, float]:
        out = {}
        for k in HORIZON_KEYS:
            out[k] = float(min(1.0, max(0.0, v.get(k, 0.5))))
        return out


# ---- Pass F: context analyst -----------------------------------------------
class SecondOrder(BaseModel):
    symbol: str
    direction: Literal["positive", "negative", "neutral"] = "neutral"
    magnitude: float = Field(0.0, ge=0.0, le=1.0)


class ContextOutput(BaseModel):
    sector_channel: str = Field("", max_length=300)
    second_order: List[SecondOrder] = Field(default_factory=list, max_length=6)
    adjustment_bps: Dict[str, int] = Field(default_factory=dict)
    macro_flags: List[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="before")
    @classmethod
    def _pre(cls, d: Any) -> Any:
        if not isinstance(d, dict):
            return d
        d = dict(d)
        d["sector_channel"] = _trunc(d.get("sector_channel"), 300) or ""
        d["macro_flags"] = _trunc_list(d.get("macro_flags"), 6, 60)
        so = d.get("second_order")
        d["second_order"] = [x for x in so if isinstance(x, dict) and x.get("symbol")][:6] if isinstance(so, list) else []
        for x in d["second_order"]:
            direction = str(x.get("direction", "")).lower()
            try:
                mag = float(x.get("magnitude", 0.0))
            except (TypeError, ValueError):
                mag = 0.0
            if mag < 0:                      # "-0.3" means negative direction, not an invalid magnitude
                direction, mag = "negative", -mag
            x["magnitude"] = min(1.0, mag)
            x["direction"] = {"up": "positive", "bullish": "positive", "down": "negative", "bearish": "negative"}.get(direction, direction)
            if x["direction"] not in ("positive", "negative", "neutral"):
                x["direction"] = "neutral"
        if not isinstance(d.get("adjustment_bps"), dict):
            d["adjustment_bps"] = {}
        return d

    @field_validator("adjustment_bps")
    @classmethod
    def _a(cls, v: Dict[str, int]) -> Dict[str, int]:
        return {k: int(max(-100, min(100, v.get(k, 0)))) for k in HORIZON_KEYS}


# ---- Pass G: synthesizer (the forecast contract, design §10) ---------------
class SynthesisOutput(BaseModel):
    symbol: str
    horizons: Dict[str, HorizonForecast] = Field(default_factory=dict)
    evidence_quality: float = Field(0.5, ge=0.0, le=1.0)
    model_disagreement: float = Field(0.0, ge=0.0, le=1.0)
    catalyst_half_life_days: int = Field(5, ge=1, le=60)
    invalidation_conditions: List[str] = Field(default_factory=list, max_length=5)
    risk_flags: List[str] = Field(default_factory=list, max_length=10)
    evidence_ids: List[str] = Field(default_factory=list, max_length=20)
    abstain: bool = False
    abstain_reason: str = Field("", max_length=200)
    rationale: str = Field("", max_length=700)

    @model_validator(mode="before")
    @classmethod
    def _pre(cls, d: Any) -> Any:
        if not isinstance(d, dict):
            return d
        d = dict(d)
        for k, n, each in (("invalidation_conditions", 5, 200), ("risk_flags", 10, 60), ("evidence_ids", 20, 60)):
            d[k] = _trunc_list(d.get(k), n, each)
        d["abstain_reason"] = _trunc(d.get("abstain_reason"), 200) or ""
        d["rationale"] = _trunc(d.get("rationale"), 700) or ""
        return d

    @field_validator("symbol")
    @classmethod
    def _up(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("horizons")
    @classmethod
    def _h(cls, v):
        return _horizons_ok(dict(v))

    @field_validator("risk_flags")
    @classmethod
    def _f(cls, v: List[str]) -> List[str]:
        return [str(x)[:60] for x in v]
